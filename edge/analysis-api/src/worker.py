"""Firebase-driven upload worker for offline WAV analysis.

Polls Firestore ``uploadJobs`` for user-uploaded .wav files, runs the
shared 4-gate bat-analysis pipeline (``bat_pipeline.run_full_pipeline``),
and writes detections back so they appear in the dashboard's Offline
WAV Analysis panel.

Key design points:

* Uses the **same pipeline module** as the Pi's live ``batdetect-service``,
  so an uploaded WAV and a mic-captured WAV traverse identical gates
  (HPF → BatDetect2 → classifier → FM-sweep → audio validator).
* Writes detections to Firestore ``batDetections`` directly (no 60s
  ``sync-service`` delay) so dashboard updates are immediate.
* Postgres is optional — when unavailable, runs in Firestore-only mode.
  Lets the worker run on the Pi *or* on any other machine (Mac, Cloud
  Run, Cloud Functions).
* On zero-detection results, writes a ``rejectionReason`` to the
  ``uploadJobs`` doc so the user sees *why* nothing was identified
  instead of a blank "0 detections" card.

Env vars — all optional, defaults match ``batdetect-service`` Pi config:

* ``FIREBASE_STORAGE_BUCKET``   — required
* ``MODEL_PATH``                — classifier checkpoint path
* ``MODEL_VERSION``             — recorded on every detection row
* ``UPLOAD_POLL_INTERVAL_SEC``  — default 5
* ``DETECTION_THRESHOLD``       — user-threshold gate (default 0.5)
* ``MIN_PREDICTION_CONF``       — classifier-confidence gate (default 0.6)
* ``HPF_ENABLED``, ``HPF_CUTOFF_HZ``, ``HPF_ORDER``
* ``VALIDATOR_ENABLED``, ``VALIDATOR_MIN_RMS``, ``VALIDATOR_MIN_SNR_DB``, ``VALIDATOR_MIN_BURST_RATIO``
* ``FM_SWEEP_ENABLED``, ``FM_SWEEP_MIN_SLOPE``, ``FM_SWEEP_MAX_LOW_BAND_RATIO``, ``FM_SWEEP_MIN_R2``
"""

import os
import time
import traceback
from datetime import datetime
from tempfile import NamedTemporaryFile
from typing import Optional

import firebase_admin
import psycopg2
from firebase_admin import credentials, firestore, storage as fb_storage
from psycopg2.extras import execute_values

from src import bat_pipeline
from src.classifier import load_groups_classifier


POLL_INTERVAL_SEC = int(os.getenv("UPLOAD_POLL_INTERVAL_SEC", "5"))
UPLOAD_JOBS_COLLECTION = os.getenv("UPLOAD_JOBS_COLLECTION", "uploadJobs")
BAT_DETECTIONS_COLLECTION = os.getenv("BAT_DETECTIONS_COLLECTION", "batDetections")
DEVICE_LABEL = os.getenv("UPLOAD_DEVICE_LABEL", "upload")

MODEL_PATH = os.getenv("MODEL_PATH", "/app/models/groups_model.pt")
MODEL_VERSION = os.getenv("MODEL_VERSION", "groups_v1_post_epfu_partial_2026-04-17")


def _load_pipeline_cfg() -> dict:
    """Read every pipeline knob from env. Defaults match Pi live config."""
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
#  Infra init
# ---------------------------------------------------------------------------

def init_firebase():
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/app/serviceAccountKey.json")
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    config = {}
    bucket = os.getenv("FIREBASE_STORAGE_BUCKET")
    if bucket:
        config["storageBucket"] = bucket

    firebase_admin.initialize_app(cred, config)
    return firestore.client()


def get_db_connection():
    """Open a Postgres connection, or return None if the DB is unreachable.

    Postgres is optional for the worker: the dashboard reads detections
    from Firestore, which we always write. Skipping the Postgres write
    lets the worker run anywhere (Mac, Cloud Run, Cloud Functions)
    without needing the Pi's local DB.
    """
    try:
        return psycopg2.connect(
            host=os.getenv("DB_HOST", "db"),
            dbname=os.getenv("DB_NAME", "soundscape"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "changeme"),
            connect_timeout=3,
        )
    except Exception as e:
        print(f"[WORKER] Postgres unavailable ({e}) — running in Firestore-only mode")
        return None


def ensure_connection(conn):
    if conn is None:
        return None
    try:
        conn.cursor().execute("SELECT 1")
        return conn
    except Exception:
        print("[WORKER] DB connection lost — reconnecting")
        try:
            conn.close()
        except Exception:
            pass
        return get_db_connection()


# ---------------------------------------------------------------------------
#  Job selection + status transitions
# ---------------------------------------------------------------------------

def _claim_next_pending_job(db):
    pending = (
        db.collection(UPLOAD_JOBS_COLLECTION)
        .where("status", "==", "pending")
        .order_by("createdAt")
        .limit(1)
        .stream()
    )
    first = next(pending, None)
    if first is None:
        return None
    job_ref = first.reference
    job_ref.update({
        "status": "processing",
        "processingStartedAt": firestore.SERVER_TIMESTAMP,
    })
    return job_ref, first.to_dict()


def _mark_error(job_ref, message: str):
    job_ref.update({
        "status": "error",
        "errorMessage": message[:500],
        "completedAt": firestore.SERVER_TIMESTAMP,
    })


def _mark_done(
    job_ref,
    detections,
    duration_seconds: float,
    *,
    rejection_reason: Optional[str] = None,
    rejection_message: Optional[str] = None,
    stats: Optional[dict] = None,
    pipeline_version: str = bat_pipeline.PIPELINE_VERSION,
):
    species_found = sorted({
        d.get("predicted_class") or d.get("species") or "Unknown"
        for d in detections
    })
    payload = {
        "status": "done",
        "detectionCount": len(detections),
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
    job_ref.update(payload)


# ---------------------------------------------------------------------------
#  Storage I/O
# ---------------------------------------------------------------------------

def _download_wav(bucket, job_id: str, dest_path: str) -> None:
    blob = bucket.blob(f"uploads/{job_id}.wav")
    if not blob.exists():
        raise FileNotFoundError(f"uploads/{job_id}.wav not found in Storage")
    blob.download_to_filename(dest_path)


# ---------------------------------------------------------------------------
#  Persistence — flatten the pipeline's (det, pred) tuples into the
#  same row shape live captures produce, then write Postgres + Firestore.
# ---------------------------------------------------------------------------

def _flatten_detection(det: dict, pred: dict, pipeline_version: str) -> dict:
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


def _persist_detections(conn, db, job_id: str, pairs, detection_time, pipeline_version: str):
    """Write detections to Postgres (synced=TRUE) + Firestore batDetections."""
    if not pairs:
        return

    detections = [_flatten_detection(d, p, pipeline_version) for d, p in pairs]

    if conn is not None:
        pg_rows = [
            (
                d["species"], d["common_name"], d["detection_prob"],
                d["start_time"], d["end_time"], d["low_freq"],
                d["high_freq"], d["duration_ms"],
                DEVICE_LABEL, job_id, detection_time, "upload",
                d["predicted_class"], d["prediction_confidence"], d["model_version"],
                True,
            )
            for d in detections
        ]
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO bat_detections
                    (species, common_name, detection_prob, start_time,
                     end_time, low_freq, high_freq, duration_ms,
                     device, sync_id, detection_time, source,
                     predicted_class, prediction_confidence, model_version,
                     synced)
                VALUES %s
            """, pg_rows)
        conn.commit()

    # Firestore — same shape as sync_bat_detections writes for live
    # captures, so the dashboard treats upload rows uniformly.
    batch = db.batch()
    for d in detections:
        doc_ref = db.collection(BAT_DETECTIONS_COLLECTION).document()
        batch.set(doc_ref, {
            "species": d["species"],
            "commonName": d["common_name"],
            "detectionProb": d["detection_prob"],
            "startTime": d["start_time"],
            "endTime": d["end_time"],
            "lowFreq": d["low_freq"],
            "highFreq": d["high_freq"],
            "durationMs": d["duration_ms"],
            "device": DEVICE_LABEL,
            "syncId": job_id,
            "detectionTime": detection_time,
            "source": "upload",
            "predictedClass": d["predicted_class"],
            "predictionConfidence": d["prediction_confidence"],
            "modelVersion": d["model_version"],
            "pipelineVersion": d["pipeline_version"],
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


# ---------------------------------------------------------------------------
#  Per-job pipeline
# ---------------------------------------------------------------------------

def process_job(conn, db, bucket, classifier_model, classifier_ckpt, pipeline_cfg, job_ref, job_data):
    job_id = job_ref.id
    filename = job_data.get("filename", "unknown.wav")
    print(f"[WORKER] Processing job {job_id} ({filename})")

    with NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        _download_wav(bucket, job_id, tmp_path)

        detection_time = datetime.utcnow()
        result = bat_pipeline.run_full_pipeline(
            tmp_path, classifier_model, classifier_ckpt, **pipeline_cfg,
        )

        if result.detections:
            _persist_detections(
                conn, db, job_id, result.detections,
                detection_time, result.pipeline_version,
            )
            _mark_done(
                job_ref, result.detections, result.duration_seconds,
                stats=result.stats,
                pipeline_version=result.pipeline_version,
            )
            print(
                f"[WORKER] Job {job_id}: {len(result.detections)} detections "
                f"({result.duration_seconds:.1f}s audio, stats={result.stats})"
            )
        else:
            human = bat_pipeline.humanize_rejection(result.rejection_reason)
            _mark_done(
                job_ref, [], result.duration_seconds,
                rejection_reason=result.rejection_reason,
                rejection_message=human,
                stats=result.stats,
                pipeline_version=result.pipeline_version,
            )
            print(
                f"[WORKER] Job {job_id}: 0 detections "
                f"(reason={result.rejection_reason}, stats={result.stats})"
            )
    except Exception as e:
        print(f"[WORKER] Job {job_id} failed: {e}")
        traceback.print_exc()
        _mark_error(job_ref, f"{type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
#  Main loop
# ---------------------------------------------------------------------------

def main():
    print("[WORKER] Initializing Firebase...")
    db = init_firebase()
    bucket = fb_storage.bucket()
    print(f"[WORKER] Firebase connected — bucket={bucket.name}")

    print(f"[WORKER] Loading groups classifier from {MODEL_PATH}")
    classifier_model, classifier_ckpt = load_groups_classifier(MODEL_PATH)
    print(
        f"[WORKER] Classifier ready: {classifier_ckpt['class_names']} "
        f"(model_version={MODEL_VERSION})"
    )

    pipeline_cfg = _load_pipeline_cfg()
    print(f"[WORKER] Pipeline config: {pipeline_cfg}")
    print(f"[WORKER] Pipeline version: {bat_pipeline.PIPELINE_VERSION}")

    conn = get_db_connection()
    mode = "Firestore + Postgres" if conn else "Firestore-only"
    print(f"[WORKER] Ready ({mode}). Polling every {POLL_INTERVAL_SEC}s...")

    while True:
        try:
            conn = ensure_connection(conn)
            claimed = _claim_next_pending_job(db)
            if claimed:
                job_ref, job_data = claimed
                process_job(
                    conn, db, bucket,
                    classifier_model, classifier_ckpt, pipeline_cfg,
                    job_ref, job_data,
                )
                continue
            time.sleep(POLL_INTERVAL_SEC)
        except Exception as e:
            print(f"[WORKER] Loop error: {e}")
            traceback.print_exc()
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
