import os
import time
from datetime import datetime

import firebase_admin
import psycopg2
from firebase_admin import credentials, firestore

from src.health import collect_all_metrics


def init_firebase():
    """Initialize Firebase Admin SDK."""
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/app/serviceAccountKey.json")
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        # Fall back to project ID only (for environments with default credentials)
        project_id = os.getenv("FIREBASE_PROJECT_ID")
        cred = credentials.ApplicationDefault()

    config = {}
    storage_bucket = os.getenv("FIREBASE_STORAGE_BUCKET")
    if storage_bucket:
        config["storageBucket"] = storage_bucket

    firebase_admin.initialize_app(cred, config)
    return firestore.client()


def get_db_connection():
    """Get PostgreSQL connection."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def sync_classifications(conn, db):
    """Sync unsynced classification records to Firestore."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, label, score, spl, device, sync_id, sync_time
            FROM classifications
            WHERE synced = FALSE
            ORDER BY sync_time ASC
            LIMIT 500
        """)
        rows = cur.fetchall()

    if not rows:
        return 0

    batch = db.batch()
    ids_to_mark = []

    for row in rows:
        doc_ref = db.collection("classifications").document()
        batch.set(doc_ref, {
            "label": row[1],
            "score": row[2],
            "spl": row[3],
            "device": row[4],
            "syncId": row[5],
            "syncTime": row[6],
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        ids_to_mark.append(row[0])

    batch.commit()

    # Mark as synced
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE classifications SET synced = TRUE WHERE id = ANY(%s)",
            (ids_to_mark,)
        )
    conn.commit()

    return len(rows)


def sync_bat_detections(conn, db):
    """Sync unsynced bat detection records to Firestore."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, species, common_name, detection_prob,
                   start_time, end_time, low_freq, high_freq,
                   duration_ms, device, sync_id, detection_time
            FROM bat_detections
            WHERE synced = FALSE
            ORDER BY detection_time ASC
            LIMIT 500
        """)
        rows = cur.fetchall()

    if not rows:
        return 0

    batch = db.batch()
    ids_to_mark = []

    for row in rows:
        doc_ref = db.collection("batDetections").document()
        batch.set(doc_ref, {
            "species": row[1],
            "commonName": row[2],
            "detectionProb": row[3],
            "startTime": row[4],
            "endTime": row[5],
            "lowFreq": row[6],
            "highFreq": row[7],
            "durationMs": row[8],
            "device": row[9],
            "syncId": row[10],
            "detectionTime": row[11],
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        ids_to_mark.append(row[0])

    batch.commit()

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bat_detections SET synced = TRUE WHERE id = ANY(%s)",
            (ids_to_mark,)
        )
    conn.commit()

    return len(rows)


# ---------------------------------------------------------------------------
#  Database migrations (idempotent — safe to run every startup)
# ---------------------------------------------------------------------------

def run_migrations(conn):
    """Create tables and columns that may not exist on older databases."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS device_status (
                id SERIAL PRIMARY KEY,
                uptime_seconds FLOAT,
                cpu_temp FLOAT,
                cpu_load_1m FLOAT,
                cpu_load_5m FLOAT,
                cpu_load_15m FLOAT,
                mem_total_mb FLOAT,
                mem_available_mb FLOAT,
                disk_total_gb FLOAT,
                disk_used_gb FLOAT,
                internet_connected BOOLEAN DEFAULT FALSE,
                internet_latency_ms FLOAT,
                audiomoth_connected BOOLEAN DEFAULT FALSE,
                capture_errors_1h INTEGER DEFAULT 0,
                db_size_mb FLOAT,
                classifications_total INTEGER DEFAULT 0,
                bat_detections_total INTEGER DEFAULT 0,
                unsynced_count INTEGER DEFAULT 0,
                recorded_at TIMESTAMP NOT NULL DEFAULT NOW(),
                synced BOOLEAN DEFAULT FALSE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS capture_errors (
                id SERIAL PRIMARY KEY,
                service VARCHAR(50) NOT NULL,
                error_type VARCHAR(100),
                message TEXT,
                recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        # Add audio columns to bat_detections if they don't exist yet
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'bat_detections' AND column_name = 'audio_path'
                ) THEN
                    ALTER TABLE bat_detections ADD COLUMN audio_path TEXT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'bat_detections' AND column_name = 'audio_url'
                ) THEN
                    ALTER TABLE bat_detections ADD COLUMN audio_url TEXT;
                END IF;
            END $$;
        """)
    conn.commit()
    print("[SYNC] Database migrations complete")


# ---------------------------------------------------------------------------
#  Device health
# ---------------------------------------------------------------------------

def sync_device_status(conn, db):
    """Collect device metrics, store locally, and push to Firestore."""
    try:
        metrics = collect_all_metrics(conn)

        # Insert into local DB
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO device_status (
                    uptime_seconds, cpu_temp, cpu_load_1m, cpu_load_5m, cpu_load_15m,
                    mem_total_mb, mem_available_mb, disk_total_gb, disk_used_gb,
                    internet_connected, internet_latency_ms, audiomoth_connected,
                    capture_errors_1h, db_size_mb, classifications_total,
                    bat_detections_total, unsynced_count, synced
                ) VALUES (
                    %(uptime_seconds)s, %(cpu_temp)s, %(cpu_load_1m)s, %(cpu_load_5m)s,
                    %(cpu_load_15m)s, %(mem_total_mb)s, %(mem_available_mb)s,
                    %(disk_total_gb)s, %(disk_used_gb)s, %(internet_connected)s,
                    %(internet_latency_ms)s, %(audiomoth_connected)s,
                    %(capture_errors_1h)s, %(db_size_mb)s, %(classifications_total)s,
                    %(bat_detections_total)s, %(unsynced_count)s, TRUE
                )
            """, metrics)
        conn.commit()

        # Overwrite a single Firestore document for the edge device
        doc_ref = db.collection("deviceStatus").document("edge-device")
        doc_ref.set({
            "uptimeSeconds": metrics["uptime_seconds"],
            "cpuTemp": metrics["cpu_temp"],
            "cpuLoad1m": metrics["cpu_load_1m"],
            "cpuLoad5m": metrics["cpu_load_5m"],
            "cpuLoad15m": metrics["cpu_load_15m"],
            "memTotalMb": metrics["mem_total_mb"],
            "memAvailableMb": metrics["mem_available_mb"],
            "diskTotalGb": metrics["disk_total_gb"],
            "diskUsedGb": metrics["disk_used_gb"],
            "internetConnected": metrics["internet_connected"],
            "internetLatencyMs": metrics["internet_latency_ms"],
            "audiomothConnected": metrics["audiomoth_connected"],
            "captureErrors1h": metrics["capture_errors_1h"],
            "dbSizeMb": metrics["db_size_mb"],
            "classificationsTotal": metrics["classifications_total"],
            "batDetectionsTotal": metrics["bat_detections_total"],
            "unsyncedCount": metrics["unsynced_count"],
            "recordedAt": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"[SYNC] Device status error: {e}")


# ---------------------------------------------------------------------------
#  Bat audio upload (optional — controlled by UPLOAD_BAT_AUDIO env var)
# ---------------------------------------------------------------------------

def upload_bat_audio(conn, db):
    """Upload saved bat-call .wav files to Firebase Storage.

    Only runs when UPLOAD_BAT_AUDIO=true.  The batdetect-service saves
    .wav files to /bat_audio/ when it detects a bat call.
    """
    if os.getenv("UPLOAD_BAT_AUDIO", "false").lower() != "true":
        return 0

    try:
        from firebase_admin import storage as fb_storage
        bucket = fb_storage.bucket()
    except Exception as e:
        print(f"[SYNC] Firebase Storage not available: {e}")
        return 0

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, audio_path, sync_id
            FROM bat_detections
            WHERE audio_path IS NOT NULL AND audio_url IS NULL
            LIMIT 10
        """)
        rows = cur.fetchall()

    if not rows:
        return 0

    uploaded = 0
    for det_id, audio_path, sync_id in rows:
        if not audio_path or not os.path.exists(audio_path):
            continue
        try:
            blob = bucket.blob(f"bat_audio/{sync_id}.wav")
            blob.upload_from_filename(audio_path)
            blob.make_public()
            audio_url = blob.public_url

            # Update the Firestore document
            docs = (
                db.collection("batDetections")
                .where("syncId", "==", sync_id)
                .limit(1)
                .get()
            )
            for d in docs:
                d.reference.update({"audioUrl": audio_url})

            # Update local DB
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bat_detections SET audio_url = %s WHERE id = %s",
                    (audio_url, det_id),
                )
            conn.commit()

            # Remove local copy
            os.remove(audio_path)
            uploaded += 1
        except Exception as e:
            print(f"[SYNC] Failed to upload audio for detection {det_id}: {e}")

    return uploaded


# ---------------------------------------------------------------------------
#  Data retention (keeps the Pi SD card lean)
# ---------------------------------------------------------------------------

def cleanup_old_data(conn):
    """Delete synced records older than 30 days and old health/error rows."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM classifications "
                "WHERE synced = TRUE AND sync_time < NOW() - INTERVAL '30 days'"
            )
            c1 = cur.rowcount
            cur.execute(
                "DELETE FROM bat_detections "
                "WHERE synced = TRUE AND detection_time < NOW() - INTERVAL '30 days'"
            )
            c2 = cur.rowcount
            cur.execute(
                "DELETE FROM device_status "
                "WHERE recorded_at < NOW() - INTERVAL '7 days'"
            )
            cur.execute(
                "DELETE FROM capture_errors "
                "WHERE recorded_at < NOW() - INTERVAL '7 days'"
            )
        conn.commit()
        if c1 or c2:
            print(f"[SYNC] Retention cleanup: {c1} classifications, {c2} bat detections")
    except Exception as e:
        print(f"[SYNC] Retention cleanup error: {e}")


def main():
    sync_interval = int(os.getenv("SYNC_INTERVAL", "60"))

    print("[SYNC] Initializing Firebase...")
    db = init_firebase()
    print("[SYNC] Firebase connected")

    # Run idempotent migrations on startup
    try:
        conn = get_db_connection()
        run_migrations(conn)
        conn.close()
    except Exception as e:
        print(f"[SYNC] Migration warning: {e}")

    print(f"[SYNC] Starting sync loop (interval: {sync_interval}s)")

    cycle = 0
    while True:
        try:
            conn = get_db_connection()

            class_count = sync_classifications(conn, db)
            bat_count = sync_bat_detections(conn, db)
            audio_count = upload_bat_audio(conn, db)
            sync_device_status(conn, db)

            if class_count > 0 or bat_count > 0:
                print(f"[SYNC] Synced {class_count} classifications, {bat_count} bat detections "
                      f"at {datetime.utcnow().isoformat()}")
            if audio_count > 0:
                print(f"[SYNC] Uploaded {audio_count} bat audio file(s)")

            # Run retention cleanup once per hour (every 60 cycles at 60s interval)
            cycle += 1
            if cycle % 60 == 0:
                cleanup_old_data(conn)

            conn.close()

        except Exception as e:
            print(f"[SYNC] Error: {e}")

        time.sleep(sync_interval)


if __name__ == "__main__":
    main()
