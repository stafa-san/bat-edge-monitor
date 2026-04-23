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


def _get_classifier():
    """Lazy-load ``torch`` + ``batdetect2`` + the classifier checkpoint.

    Kept out of module scope so the Firebase CLI can parse this file
    locally without the heavy ML deps installed.
    """
    global _classifier_cache
    if _classifier_cache is None:
        from src.classifier import load_groups_classifier  # noqa: E402

        model_path = os.getenv(
            "MODEL_PATH",
            os.path.join(os.path.dirname(__file__), "models", "groups_model.pt"),
        )
        print(f"[CF] Loading classifier from {model_path}")
        _classifier_cache = load_groups_classifier(model_path)
        ckpt = _classifier_cache[1]
        print(f"[CF] Classifier ready: {ckpt['class_names']}")
    return _classifier_cache


def _load_pipeline_cfg() -> dict:
    """Pipeline knobs — defaults match the Pi's live batdetect-service."""
    return {
        "user_threshold": float(os.getenv("DETECTION_THRESHOLD", "0.5")),
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
    job_ref.update(payload)


def _render_and_upload_spectrogram(bucket, wav_path: str, job_id: str, pairs, filename: str) -> Optional[str]:
    """Generate a labelled PNG spectrogram and upload it to Firebase Storage.

    Returns the download URL on success, None on any failure (never
    raises — spectrogram is nice-to-have, not critical to the job).
    """
    try:
        import urllib.parse
        import uuid
        from batdetect2 import api as bat_api
        from src.spectrogram import generate_spectrogram

        audio = bat_api.load_audio(wav_path)
        sr = int(bat_api.get_config().get("target_samp_rate", 256000))

        png_path = wav_path + ".spectrogram.png"
        generate_spectrogram(audio, sr, pairs, png_path, title=filename)

        blob = bucket.blob(f"spectrograms/{job_id}.png")
        token = uuid.uuid4().hex
        blob.metadata = {"firebaseStorageDownloadTokens": token}
        blob.upload_from_filename(png_path, content_type="image/png")
        # patch() persists the metadata so the token is attached.
        blob.patch()

        try:
            os.unlink(png_path)
        except OSError:
            pass

        encoded = urllib.parse.quote(blob.name, safe="")
        return (
            f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}"
            f"/o/{encoded}?alt=media&token={token}"
        )
    except Exception as e:
        print(f"[CF] spectrogram render/upload failed for {job_id}: {e}")
        return None


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

        # Spectrogram — rendered for every outcome (even rejected
        # segments) so advisors can see *why* the pipeline decided what
        # it decided.
        spectrogram_url = _render_and_upload_spectrogram(
            bucket, tmp_path, job_id, result.detections, filename,
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
