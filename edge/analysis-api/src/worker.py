"""Firebase-driven upload worker for offline WAV analysis.

Replaces the old HTTP API with a polling loop so uploads can originate
from *anywhere* (not just the LAN). Flow:

  1. Dashboard uploads a .wav to Firebase Storage at ``uploads/{jobId}.wav``
     and creates a Firestore doc ``uploadJobs/{jobId}`` with ``status='pending'``.
  2. This worker polls ``uploadJobs`` every few seconds, claims the oldest
     pending job, downloads the WAV, runs BatDetect2 + groups classifier.
  3. Detection rows are written to Postgres (``source='upload'``, ``synced=TRUE``)
     AND directly to Firestore ``batDetections`` so the dashboard sees them
     instantly — bypasses the sync-service's 60s cadence for uploads.
  4. ``uploadJobs/{jobId}`` transitions to ``status='done'`` (or ``'error'``)
     with a summary the dashboard can render without re-querying.

The legacy FastAPI HTTP app in ``main.py`` stays as dead code. Flip the
Dockerfile ``CMD`` back to ``uvicorn src.main:app`` if you ever want LAN
HTTP uploads again.

Env vars (all optional):
  * ``FIREBASE_STORAGE_BUCKET``      — required; same bucket as sync-service
  * ``UPLOAD_POLL_INTERVAL_SEC``     — default 5
  * ``UPLOAD_JOBS_COLLECTION``       — default 'uploadJobs'
  * ``BAT_DETECTIONS_COLLECTION``    — default 'batDetections'
  * ``UPLOAD_DEVICE_LABEL``          — default 'upload'
"""

import os
import time
import traceback
import uuid
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile

import firebase_admin
import psycopg2
import soundfile as sf
from firebase_admin import credentials, firestore, storage as fb_storage
from psycopg2.extras import execute_values

# ``run_batdetect`` in main.py is self-contained — importing it does not
# start a FastAPI server. We keep the HTTP module intact as a fallback.
from src.main import run_batdetect  # noqa: E402


POLL_INTERVAL_SEC = int(os.getenv("UPLOAD_POLL_INTERVAL_SEC", "5"))
UPLOAD_JOBS_COLLECTION = os.getenv("UPLOAD_JOBS_COLLECTION", "uploadJobs")
BAT_DETECTIONS_COLLECTION = os.getenv("BAT_DETECTIONS_COLLECTION", "batDetections")
DEVICE_LABEL = os.getenv("UPLOAD_DEVICE_LABEL", "upload")


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
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def ensure_connection(conn):
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
    """Return (job_doc_ref, job_data) for the oldest pending job, or None.

    Not strictly atomic across multiple workers — but we run one worker
    per Pi, so the race is theoretical.
    """
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


def _mark_done(job_ref, detections, duration_seconds):
    species_found = sorted({
        d.get("predicted_class") or d.get("species") or "Unknown"
        for d in detections
    })
    job_ref.update({
        "status": "done",
        "detectionCount": len(detections),
        "speciesFound": species_found,
        "durationSeconds": round(duration_seconds, 2),
        "completedAt": firestore.SERVER_TIMESTAMP,
    })


# ---------------------------------------------------------------------------
#  Storage I/O
# ---------------------------------------------------------------------------

def _download_wav(bucket, job_id: str, dest_path: str) -> None:
    blob = bucket.blob(f"uploads/{job_id}.wav")
    if not blob.exists():
        raise FileNotFoundError(f"uploads/{job_id}.wav not found in Storage")
    blob.download_to_filename(dest_path)


# ---------------------------------------------------------------------------
#  Persistence
# ---------------------------------------------------------------------------

def _persist_detections(conn, db, job_id: str, detections, detection_time, duration_seconds):
    """Write detections to Postgres (synced=TRUE) + Firestore batDetections.

    Double-writing Firestore directly from the worker sidesteps the
    sync-service's 60s cycle for upload results. sync-service would
    otherwise skip these rows anyway because ``synced=TRUE``.
    """
    if not detections:
        return

    pg_rows = [
        (
            d["species"], d["common_name"], d["detection_prob"],
            d["start_time"], d["end_time"], d["low_freq"],
            d["high_freq"], d["duration_ms"],
            DEVICE_LABEL, job_id, detection_time, "upload",
            d.get("predicted_class"),
            d.get("prediction_confidence"),
            d.get("model_version"),
            True,  # synced — worker already wrote to Firestore below
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

    # Mirror into Firestore now so the dashboard updates without waiting
    # for the sync-service cycle. Shape mirrors sync_bat_detections().
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
            "predictedClass": d.get("predicted_class"),
            "predictionConfidence": d.get("prediction_confidence"),
            "modelVersion": d.get("model_version"),
            # Fields present on live-source rows but not meaningful for
            # uploads; write explicit nulls so dashboard shape matches.
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

def process_job(conn, db, bucket, job_ref, job_data):
    job_id = job_ref.id
    filename = job_data.get("filename", "unknown.wav")
    print(f"[WORKER] Processing job {job_id} ({filename})")

    with NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        _download_wav(bucket, job_id, tmp_path)

        audio, sr = sf.read(tmp_path, dtype="float32")
        duration_s = (len(audio) / sr) if sr else 0.0

        detections = run_batdetect(tmp_path)
        detection_time = datetime.utcnow()

        _persist_detections(conn, db, job_id, detections, detection_time, duration_s)
        _mark_done(job_ref, detections, duration_s)
        print(f"[WORKER] Job {job_id}: {len(detections)} detections, {duration_s:.1f}s audio")
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

    conn = get_db_connection()
    print(f"[WORKER] Postgres connected. Polling every {POLL_INTERVAL_SEC}s...")

    while True:
        try:
            conn = ensure_connection(conn)
            claimed = _claim_next_pending_job(db)
            if claimed:
                job_ref, job_data = claimed
                process_job(conn, db, bucket, job_ref, job_data)
                continue  # check for more pending jobs immediately
            time.sleep(POLL_INTERVAL_SEC)
        except Exception as e:
            print(f"[WORKER] Loop error: {e}")
            traceback.print_exc()
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
