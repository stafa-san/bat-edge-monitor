import asyncio
import os
import signal
import sys
import uuid
from datetime import datetime

import librosa
import psycopg2
from psycopg2.extras import execute_values

from src.audio_device import AudioDevice
from src.classifier import AudioClassifier
from src.spl import calculate_sound_pressure_level


def get_db_connection():
    """Create a new Postgres connection."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def ensure_connection(conn):
    """Return *conn* if it's alive, otherwise create a fresh connection."""
    try:
        conn.cursor().execute("SELECT 1")
        return conn
    except Exception:
        print("[AST] DB connection lost — reconnecting")
        try:
            conn.close()
        except Exception:
            pass
        return get_db_connection()


def flush_buffer(conn, buffer):
    """Write pending rows to Postgres and clear the buffer."""
    if not buffer:
        return conn
    conn = ensure_connection(conn)
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO classifications (label, score, spl, device, sync_id, sync_time)
                VALUES %s
            """, buffer)
        conn.commit()
        print(f"[AST] Flushed {len(buffer)} records to local DB")
        buffer.clear()
    except Exception as e:
        print(f"[AST] Flush failed: {e}")
    return conn


async def main():
    device_id = os.getenv("DEVICE_NAME", "AudioMoth")
    sample_rate = int(os.getenv("SAMPLE_RATE", "192000"))
    ast_sample_rate = 16000

    print(f"[AST] Initializing audio device: {device_id}")
    audio = AudioDevice(name=device_id, sampling_rate=sample_rate)

    print("[AST] Loading AST model (this may take a minute on first run)...")
    classifier = AudioClassifier()
    print("[AST] Model loaded successfully")

    conn = get_db_connection()

    print(f"[AST] Monitoring started - device: {audio.name}, rate: {sample_rate} Hz")

    buffer = []
    sample_count = 0

    # ── Flush buffer on SIGTERM (docker stop) ──
    def handle_shutdown(signum, frame):
        print(f"[AST] Received signal {signum} — flushing {len(buffer)} buffered rows")
        try:
            flush_buffer(conn, buffer)
        except Exception as e:
            print(f"[AST] Shutdown flush failed: {e}")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    async for sample in audio.continuous_capture(sample_duration=1, capture_delay=0):
        try:
            sample_count += 1
            sample_16k = librosa.resample(sample, orig_sr=sample_rate, target_sr=ast_sample_rate)
            predictions = await classifier.predict(sample_16k, top_k=5)
            spl = await calculate_sound_pressure_level(sample)

            sync_id = str(uuid.uuid4())
            sync_time = datetime.utcnow()

            top_label = predictions.iloc[0]
            print(f"[AST] #{sample_count} | {top_label['label']}: {top_label['score']:.3f} | SPL: {spl:.1f} dB")

            for _, row in predictions.iterrows():
                buffer.append((
                    row['label'], float(row['score']), spl,
                    device_id, sync_id, sync_time
                ))

            if len(buffer) >= 25:
                conn = flush_buffer(conn, buffer)

        except Exception as e:
            print(f"[AST] Error processing sample #{sample_count}: {e}")
            conn = ensure_connection(conn)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO capture_errors (service, error_type, message) "
                        "VALUES (%s, %s, %s)",
                        ("ast-service", type(e).__name__, str(e)[:500]),
                    )
                conn.commit()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
