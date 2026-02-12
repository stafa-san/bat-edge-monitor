import os
import time
from datetime import datetime

import firebase_admin
import psycopg2
from firebase_admin import credentials, firestore


def init_firebase():
    """Initialize Firebase Admin SDK."""
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/app/serviceAccountKey.json")
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        # Fall back to project ID only (for environments with default credentials)
        project_id = os.getenv("FIREBASE_PROJECT_ID")
        cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
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


def main():
    sync_interval = int(os.getenv("SYNC_INTERVAL", "60"))

    print("[SYNC] Initializing Firebase...")
    db = init_firebase()
    print("[SYNC] Firebase connected")

    print(f"[SYNC] Starting sync loop (interval: {sync_interval}s)")

    while True:
        try:
            conn = get_db_connection()

            class_count = sync_classifications(conn, db)
            bat_count = sync_bat_detections(conn, db)

            if class_count > 0 or bat_count > 0:
                print(f"[SYNC] Synced {class_count} classifications, {bat_count} bat detections "
                      f"at {datetime.utcnow().isoformat()}")

            conn.close()

        except Exception as e:
            print(f"[SYNC] Error: {e}")

        time.sleep(sync_interval)


if __name__ == "__main__":
    main()
