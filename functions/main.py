"""Firebase Cloud Function — offline WAV analysis.

Fires on Firestore ``uploadJobs/{jobId}`` document creation. Downloads
the corresponding ``uploads/{jobId}.wav`` from Firebase Storage, runs
the shared 4-gate bat-analysis pipeline (``src.bat_pipeline.run_full_pipeline``),
and writes detections back to Firestore so the dashboard's Offline
WAV Analysis panel shows them in place.

Runs the exact same pipeline as the Pi's live capture path — the
``src/`` modules are synced from ``edge/batdetect-service/src/`` by
``build.sh`` (predeploy hook in ``firebase.json``).

**Import discipline:** Firebase CLI imports this module during deploy
to discover registered function signatures. Heavy imports (``torch``,
``batdetect2``, ``scipy``) are deferred to inside the handler body so
local CLI parse only needs ``firebase-functions`` + ``firebase-admin``
in the local ``functions/venv``.

Config — set per-function via the decorator defaults below, overridable
per-deploy with env vars:

* ``MODEL_PATH``               — classifier checkpoint (bundled at deploy)
* ``MODEL_VERSION``            — recorded on every detection row
* ``FIREBASE_STORAGE_BUCKET``  — provided automatically by Firebase at runtime
* ``DETECTION_THRESHOLD``, ``MIN_PREDICTION_CONF``
* ``HPF_*``, ``VALIDATOR_*``, ``FM_SWEEP_*`` — pipeline knobs, defaults mirror Pi
"""

from __future__ import annotations

import os
import tempfile
import traceback
from datetime import datetime
from typing import Optional

import firebase_admin
from firebase_admin import firestore, storage as fb_storage
from firebase_functions import firestore_fn, options

# ---------------------------------------------------------------------------
#  Firebase init — runs once per cold start
# ---------------------------------------------------------------------------

firebase_admin.initialize_app()


# ---------------------------------------------------------------------------
#  Config (read at module load, cheap)
# ---------------------------------------------------------------------------

MODEL_VERSION = os.getenv("MODEL_VERSION", "groups_v1_post_epfu_partial_2026-04-17")
DEVICE_LABEL = os.getenv("UPLOAD_DEVICE_LABEL", "upload")
BAT_DETECTIONS_COLLECTION = os.getenv("BAT_DETECTIONS_COLLECTION", "batDetections")


# ---------------------------------------------------------------------------
#  Lazy globals — heavy imports + model load happen on first invocation.
# ---------------------------------------------------------------------------

_classifier_cache: Optional[tuple] = None
_bd_warmed_up = False


def _get_classifier():
    """Lazy-load ``torch`` + ``batdetect2`` + the classifier checkpoint.

    Kept out of module scope so the Firebase CLI can parse this file
    locally without the heavy ML deps installed.

    Also pins ``torch`` to a single inference thread and runs a BatDetect2
    warm-up forward pass on synthetic audio. Background: in the CF
    environment we observed BatDetect2 silently returning 0 raw
    detections on the SAME audio file that had just returned 26 — the
    same worker, the same HPF cache, the same classifier cache. The
    only plausible source of drift is torch-internal state (thread
    contention, JIT-compile state, lazy weight init). Pinning threads
    + forcing a full forward pass before accepting real traffic makes
    cold-starts deterministic; if the warm-up itself fails the worker
    crashes and CF restarts it, which is the outcome we want over the
    current "silently returns garbage" behaviour.
    """
    global _classifier_cache, _bd_warmed_up
    if _classifier_cache is None:
        import numpy as np
        import torch
        from batdetect2 import api as bat_api
        from src.classifier import load_groups_classifier  # noqa: E402

        # Pin single-threaded inference — CF workers share CPU with
        # other tenants and torch's intra-op threadpool is a well-known
        # source of nondeterminism on shared hosts.
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)

        model_path = os.getenv(
            "MODEL_PATH",
            os.path.join(os.path.dirname(__file__), "models", "groups_model.pt"),
        )
        print(f"[CF] Loading classifier from {model_path}")
        _classifier_cache = load_groups_classifier(model_path)
        ckpt = _classifier_cache[1]
        print(f"[CF] Classifier ready: {ckpt['class_names']}")

        # Warm-up pass through BatDetect2. We don't strictly need
        # detections on the warm-up — we only need torch to do a real
        # forward pass so the lazy weight init + any JIT tracing
        # happens while the worker is guaranteed idle. But a pure
        # sine tone doesn't exercise the detection HEAD (the detector
        # is trained to ignore carriers, only fires on FM sweeps).
        # Using a bat-like FM-chirp signal forces a full forward pass
        # through both the backbone AND the detection head, which is
        # the state we care about refreshing. Worked out the hard way
        # on the Pi — see commit that introduced this comment.
        if not _bd_warmed_up:
            from scipy.signal import chirp as _chirp
            cfg = bat_api.get_config()
            sr = int(cfg.get("target_samp_rate", 256000))
            audio = np.zeros(sr, dtype=np.float32)
            chirp_dur = 0.006
            chirp_n = int(sr * chirp_dur)
            chirp_t = np.linspace(0.0, chirp_dur, chirp_n, endpoint=False)
            chirp_sig = _chirp(
                chirp_t, f0=60_000, f1=25_000, t1=chirp_dur, method="linear",
            ).astype(np.float32)
            for i in range(5):
                start = int((0.1 + 0.15 * i) * sr)
                audio[start:start + chirp_n] += 0.5 * chirp_sig
            audio += 0.005 * np.random.randn(sr).astype(np.float32)
            try:
                bat_api.process_audio(audio, config=cfg)
                print("[CF] BatDetect2 warm-up complete")
            except Exception as e:  # noqa: BLE001 — want CF to restart
                print(f"[CF] BatDetect2 warm-up FAILED: {e}")
                raise
            _bd_warmed_up = True
    return _classifier_cache


def _load_pipeline_cfg() -> dict:
    """Pipeline knobs — defaults match the Pi's live batdetect-service."""
    return {
        "user_threshold": float(os.getenv("DETECTION_THRESHOLD", "0.3")),
        "min_pred_conf": float(os.getenv("MIN_PREDICTION_CONF", "0.6")),
        "hpf_enabled": os.getenv("HPF_ENABLED", "true").lower() == "true",
        "hpf_cutoff_hz": float(os.getenv("HPF_CUTOFF_HZ", "16000")),
        "hpf_order": int(os.getenv("HPF_ORDER", "4")),
        "validator_enabled": os.getenv("VALIDATOR_ENABLED", "true").lower() == "true",
        "validator_min_rms": float(os.getenv("VALIDATOR_MIN_RMS", "0.005")),
        "validator_min_snr_db": float(os.getenv("VALIDATOR_MIN_SNR_DB", "10.0")),
        "validator_min_burst_ratio": float(os.getenv("VALIDATOR_MIN_BURST_RATIO", "3.0")),
        "fm_sweep_enabled": os.getenv("FM_SWEEP_ENABLED", "true").lower() == "true",
        "fm_sweep_min_slope": float(os.getenv("FM_SWEEP_MIN_SLOPE", "-0.1")),
        "fm_sweep_max_low_band_ratio": float(os.getenv("FM_SWEEP_MAX_LOW_BAND_RATIO", "0.5")),
        "fm_sweep_min_r2": float(os.getenv("FM_SWEEP_MIN_R2", "0.2")),
    }


# ---------------------------------------------------------------------------
#  Persistence — Firestore only (no Postgres in the cloud)
# ---------------------------------------------------------------------------

def _flatten(det: dict, pred: dict, pipeline_version: str) -> dict:
    species = det.get("class", "Unknown")
    start = det.get("start_time", 0.0)
    end = det.get("end_time", 0.0)
    return {
        "species": species,
        "common_name": species,
        "detection_prob": round(det.get("det_prob", 0.0), 4),
        "start_time": round(start, 4),
        "end_time": round(end, 4),
        "low_freq": round(det.get("low_freq", 0.0), 1),
        "high_freq": round(det.get("high_freq", 0.0), 1),
        "duration_ms": round((end - start) * 1000, 1),
        "predicted_class": pred["predicted_class"],
        "prediction_confidence": round(pred["prediction_confidence"], 4),
        "model_version": MODEL_VERSION,
        "pipeline_version": pipeline_version,
    }


def _write_detections(db, job_id: str, pairs, detection_time, pipeline_version: str):
    if not pairs:
        return
    batch = db.batch()
    for det, pred in pairs:
        row = _flatten(det, pred, pipeline_version)
        doc_ref = db.collection(BAT_DETECTIONS_COLLECTION).document()
        batch.set(doc_ref, {
            "species": row["species"],
            "commonName": row["common_name"],
            "detectionProb": row["detection_prob"],
            "startTime": row["start_time"],
            "endTime": row["end_time"],
            "lowFreq": row["low_freq"],
            "highFreq": row["high_freq"],
            "durationMs": row["duration_ms"],
            "device": DEVICE_LABEL,
            "syncId": job_id,
            "detectionTime": detection_time,
            "source": "upload",
            "predictedClass": row["predicted_class"],
            "predictionConfidence": row["prediction_confidence"],
            "modelVersion": row["model_version"],
            "pipelineVersion": row["pipeline_version"],
            # Fields meaningful for live captures only — written explicit
            # null so dashboard row shape matches.
            "reviewedBy": None,
            "reviewedAt": None,
            "verifiedClass": None,
            "reviewerNotes": None,
            "temperatureC": None,
            "temperatureTimestamp": None,
            "alignmentErrorMs": None,
            "storageTier": None,
            "expiresAt": None,
            "remoteAudioPath": None,
            "syncedRemoteAt": None,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
    batch.commit()


def _mark_done(
    job_ref,
    pairs,
    duration_seconds: float,
    pipeline_version: str,
    *,
    rejection_reason: Optional[str] = None,
    rejection_message: Optional[str] = None,
    stats: Optional[dict] = None,
    spectrogram_url: Optional[str] = None,
    spectrogram_annotated_url: Optional[str] = None,
    spectrogram_sonobat_url: Optional[str] = None,
    spectrogram_sonobat_annotated_url: Optional[str] = None,
    time_expanded_audio_url: Optional[str] = None,
):
    species_found = sorted({
        (pred.get("predicted_class") or det.get("class") or "Unknown")
        for det, pred in pairs
    })
    payload = {
        "status": "done",
        "detectionCount": len(pairs),
        "speciesFound": species_found,
        "durationSeconds": round(duration_seconds, 2),
        "pipelineVersion": pipeline_version,
        "completedAt": firestore.SERVER_TIMESTAMP,
    }
    if rejection_reason:
        payload["rejectionReason"] = rejection_reason
        payload["rejectionMessage"] = rejection_message or rejection_reason
    if stats:
        payload["stats"] = stats
    if spectrogram_url:
        payload["spectrogramUrl"] = spectrogram_url
    if spectrogram_annotated_url:
        payload["spectrogramAnnotatedUrl"] = spectrogram_annotated_url
    if spectrogram_sonobat_url:
        payload["spectrogramSonobatUrl"] = spectrogram_sonobat_url
    if spectrogram_sonobat_annotated_url:
        payload["spectrogramSonobatAnnotatedUrl"] = spectrogram_sonobat_annotated_url
    if time_expanded_audio_url:
        payload["timeExpandedAudioUrl"] = time_expanded_audio_url
    job_ref.update(payload)


def _render_and_upload_time_expanded(bucket, wav_path: str, job_id: str, expansion: int = 10) -> Optional[str]:
    """Render a time-expanded WAV so ultrasonic bat calls become audible.

    Writes the same samples at 1/``expansion`` the sample rate — this
    pitch-shifts everything down by the expansion factor (e.g. 40 kHz
    bat call played back at 4 kHz, well within human hearing). Bat
    researchers have used this trick for decades; it's the fastest
    sanity-check for "is this actually a bat call?"

    Uploaded to ``audio/{jobId}.expanded.wav`` with a Firebase download
    token. Returns the URL or None on failure.
    """
    try:
        import urllib.parse
        import uuid
        import soundfile as sf
        from batdetect2 import api as bat_api

        audio = bat_api.load_audio(wav_path)
        sr = int(bat_api.get_config().get("target_samp_rate", 256000))
        expanded_sr = max(sr // expansion, 8000)

        expanded_path = wav_path + ".expanded.wav"
        # 16-bit PCM for wide browser / <audio> tag compatibility.
        sf.write(expanded_path, audio, expanded_sr, subtype="PCM_16")

        blob = bucket.blob(f"audio/{job_id}.expanded.wav")
        token = uuid.uuid4().hex
        blob.metadata = {"firebaseStorageDownloadTokens": token}
        blob.upload_from_filename(expanded_path, content_type="audio/wav")
        blob.patch()

        try:
            os.unlink(expanded_path)
        except OSError:
            pass

        encoded = urllib.parse.quote(blob.name, safe="")
        return (
            f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}"
            f"/o/{encoded}?alt=media&token={token}"
        )
    except Exception as e:
        print(f"[CF] time-expanded audio render/upload failed for {job_id}: {e}")
        return None


def _upload_png(bucket, local_path: str, remote_name: str) -> Optional[str]:
    """Helper: upload a PNG to Firebase Storage with a download token."""
    import urllib.parse
    import uuid

    blob = bucket.blob(remote_name)
    token = uuid.uuid4().hex
    blob.metadata = {"firebaseStorageDownloadTokens": token}
    blob.upload_from_filename(local_path, content_type="image/png")
    blob.patch()
    encoded = urllib.parse.quote(blob.name, safe="")
    return (
        f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}"
        f"/o/{encoded}?alt=media&token={token}"
    )


def _render_and_upload_spectrograms(bucket, wav_path: str, job_id: str, pairs, filename: str) -> dict:
    """Render FOUR spectrograms (2 palettes × clean/annotated) and
    upload all of them. Dashboard picks one based on the user's
    toggle state.

    Returns a dict of URLs keyed by field name — any individual URL
    can be None on failure (never raises; specs are nice-to-have, not
    critical to the job).

    Variants:
      * ``viridis_clean``    — default, perceptually-uniform palette
      * ``viridis_annotated`` — same palette, red detection boxes
      * ``sonobat_clean``    — bat-research style, no boxes
      * ``sonobat_annotated`` — bat-research style, with boxes

    The doubled upload cost (~200 KB total per job instead of 100 KB)
    is negligible under the 7-day Storage TTL.
    """
    urls: dict = {
        "viridis_clean": None,
        "viridis_annotated": None,
        "sonobat_clean": None,
        "sonobat_annotated": None,
    }
    try:
        from batdetect2 import api as bat_api
        from src.spectrogram import generate_spectrogram

        audio = bat_api.load_audio(wav_path)
        sr = int(bat_api.get_config().get("target_samp_rate", 256000))

        variants = [
            # (key, local filename, remote filename, palette, with_boxes)
            ("viridis_clean",     ".spec.v_c.png", "viridis.clean.png",     "viridis", False),
            ("viridis_annotated", ".spec.v_a.png", "viridis.annotated.png", "viridis", True),
            ("sonobat_clean",     ".spec.s_c.png", "sonobat.clean.png",     "sonobat", False),
            ("sonobat_annotated", ".spec.s_a.png", "sonobat.annotated.png", "sonobat", True),
        ]

        paths = []
        for key, local_suffix, remote_name, palette, with_boxes in variants:
            try:
                p = wav_path + local_suffix
                generate_spectrogram(
                    audio, sr, pairs, p,
                    title=filename, with_boxes=with_boxes, palette=palette,
                )
                paths.append(p)
                urls[key] = _upload_png(
                    bucket, p, f"spectrograms/{job_id}.{remote_name}",
                )
            except Exception as e:
                print(f"[CF] spectrogram {key} failed for {job_id}: {e}")

        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass
    except Exception as e:
        print(f"[CF] spectrogram render/upload failed for {job_id}: {e}")
    return urls


def _mark_error(job_ref, message: str):
    job_ref.update({
        "status": "error",
        "errorMessage": message[:500],
        "completedAt": firestore.SERVER_TIMESTAMP,
    })


# ---------------------------------------------------------------------------
#  Trigger
# ---------------------------------------------------------------------------

@firestore_fn.on_document_created(
    document="uploadJobs/{jobId}",
    region="us-central1",
    memory=options.MemoryOption.GB_4,
    cpu=2,
    timeout_sec=540,
    concurrency=1,
)
def process_upload(event: firestore_fn.Event[firestore_fn.DocumentSnapshot]) -> None:
    """Runs on every new upload job. One job per cold start typically."""
    # Heavy imports deferred here so Firebase CLI parse (which runs this
    # file's top-level code) doesn't need torch / batdetect2 installed
    # in the local venv.
    from src import bat_pipeline  # noqa: E402

    job_id = event.params["jobId"]
    snap = event.data
    if snap is None:
        print(f"[CF] {job_id}: event.data is None (doc deleted?) — skipping")
        return

    job_data = snap.to_dict() or {}
    filename = job_data.get("filename", "unknown.wav")
    print(f"[CF] Processing {job_id} ({filename})")

    db = firestore.client()
    job_ref = db.collection("uploadJobs").document(job_id)

    # Transition to processing so the dashboard spinner updates even if
    # we later crash.
    try:
        job_ref.update({
            "status": "processing",
            "processingStartedAt": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"[CF] {job_id}: failed to mark processing: {e}")

    tmp_path = None
    try:
        bucket = fb_storage.bucket()
        blob = bucket.blob(f"uploads/{job_id}.wav")
        if not blob.exists():
            raise FileNotFoundError(f"uploads/{job_id}.wav not found in Storage")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        blob.download_to_filename(tmp_path)

        classifier_model, classifier_ckpt = _get_classifier()
        pipeline_cfg = _load_pipeline_cfg()

        detection_time = datetime.utcnow()
        result = bat_pipeline.run_full_pipeline(
            tmp_path, classifier_model, classifier_ckpt, **pipeline_cfg,
        )

        # Spectrograms — rendered for every outcome (even rejected
        # segments) so advisors can see *why* the pipeline decided what
        # it decided. FOUR variants (viridis / sonobat × clean /
        # annotated) so the dashboard can toggle palette and overlay
        # independently.
        spec_urls = _render_and_upload_spectrograms(
            bucket, tmp_path, job_id, result.detections, filename,
        )
        spectrogram_url = spec_urls.get("viridis_clean")
        spectrogram_annotated_url = spec_urls.get("viridis_annotated")
        spectrogram_sonobat_url = spec_urls.get("sonobat_clean")
        spectrogram_sonobat_annotated_url = spec_urls.get("sonobat_annotated")
        # Time-expanded (10×) audio — the 40 kHz bat call becomes a
        # 4 kHz audible chirp. Ecologists rely on this to verify
        # detector output by ear.
        time_expanded_audio_url = _render_and_upload_time_expanded(
            bucket, tmp_path, job_id,
        )

        if result.detections:
            _write_detections(
                db, job_id, result.detections,
                detection_time, result.pipeline_version,
            )
            _mark_done(
                job_ref, result.detections, result.duration_seconds,
                result.pipeline_version,
                stats=result.stats,
                spectrogram_url=spectrogram_url,
                spectrogram_annotated_url=spectrogram_annotated_url,
                spectrogram_sonobat_url=spectrogram_sonobat_url,
                spectrogram_sonobat_annotated_url=spectrogram_sonobat_annotated_url,
                time_expanded_audio_url=time_expanded_audio_url,
            )
            print(
                f"[CF] {job_id}: {len(result.detections)} detections "
                f"({result.duration_seconds:.1f}s, stats={result.stats})"
            )
        else:
            human = bat_pipeline.humanize_rejection(result.rejection_reason)
            _mark_done(
                job_ref, [], result.duration_seconds,
                result.pipeline_version,
                rejection_reason=result.rejection_reason,
                rejection_message=human,
                stats=result.stats,
                spectrogram_url=spectrogram_url,
                spectrogram_annotated_url=spectrogram_annotated_url,
                spectrogram_sonobat_url=spectrogram_sonobat_url,
                spectrogram_sonobat_annotated_url=spectrogram_sonobat_annotated_url,
                time_expanded_audio_url=time_expanded_audio_url,
            )
            print(
                f"[CF] {job_id}: 0 detections "
                f"(reason={result.rejection_reason}, stats={result.stats})"
            )
    except Exception as e:
        print(f"[CF] {job_id}: failed — {e}")
        traceback.print_exc()
        try:
            _mark_error(job_ref, f"{type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
