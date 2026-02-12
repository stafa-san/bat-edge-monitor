import asyncio
import os
import uuid
from datetime import datetime

import librosa
import psycopg2
from psycopg2.extras import execute_values

from src.audio_device import AudioDevice
from src.classifier import AudioClassifier
from src.spl import calculate_sound_pressure_level


async def main():
    device_id = os.getenv("DEVICE_NAME", "AudioMoth")
    sample_rate = int(os.getenv("SAMPLE_RATE", "192000"))
    ast_sample_rate = 16000

    print(f"[AST] Initializing audio device: {device_id}")
    audio = AudioDevice(name=device_id, sampling_rate=sample_rate)

    print("[AST] Loading AST model (this may take a minute on first run)...")
    classifier = AudioClassifier()
    print("[AST] Model loaded successfully")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )

    print(f"[AST] Monitoring started - device: {audio.name}, rate: {sample_rate} Hz")

    buffer = []
    sample_count = 0

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
                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO classifications (label, score, spl, device, sync_id, sync_time)
                        VALUES %s
                    """, buffer)
                conn.commit()
                print(f"[AST] Synced {len(buffer)} records to local DB")
                buffer.clear()

        except Exception as e:
            print(f"[AST] Error processing sample #{sample_count}: {e}")
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
