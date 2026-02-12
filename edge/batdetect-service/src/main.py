import asyncio
import fcntl
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from tempfile import TemporaryDirectory

import numpy as np
import psycopg2
from psycopg2.extras import execute_values
from batdetect2 import api as bat_api

LOCK_PATH = "/locks/audio_device.lock"
UPLOAD_BAT_AUDIO = os.getenv("UPLOAD_BAT_AUDIO", "false").lower() == "true"
BAT_AUDIO_DIR = "/bat_audio"


class BatAudioCapture:
    """Captures longer audio segments optimized for bat detection."""

    def __init__(self, device_name: str, sampling_rate: int = 192000):
        self.device = self._match_device(device_name)
        self.sampling_rate = sampling_rate

    @staticmethod
    def _match_device(name: str) -> str:
        lines = subprocess.check_output(['arecord', '-l'], text=True).splitlines()
        devices = [
            f'plughw:{m.group(1)},{m.group(2)}'
            for line in lines
            if name.lower() in line.lower()
            if (m := re.search(r'card (\d+):.*device (\d+):', line))
        ]
        if not devices:
            raise ValueError(f'No devices found matching `{name}`')
        return devices[0]

    async def capture_segment(self, duration: int = 5) -> str:
        """Capture audio and return path to wav file."""
        self._temp_dir = TemporaryDirectory()
        temp_file = f'{self._temp_dir.name}/bat_audio.wav'
        command = (
            f'arecord -d {duration} -D {self.device} '
            f'-f S16_LE -r {self.sampling_rate} '
            f'-c 1 -q {temp_file}'
        )
        lock_fd = open(LOCK_PATH, 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            subprocess.check_call(command, shell=True)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        return temp_file


async def main():
    device_name = os.getenv("DEVICE_NAME", "AudioMoth")
    sample_rate = int(os.getenv("SAMPLE_RATE", "192000"))
    threshold = float(os.getenv("DETECTION_THRESHOLD", "0.3"))
    segment_duration = int(os.getenv("SEGMENT_DURATION", "5"))

    print(f"[BAT] Initializing audio capture: {device_name}")
    capture = BatAudioCapture(device_name=device_name, sampling_rate=sample_rate)

    print("[BAT] Loading BatDetect2 model...")
    # Warm up the model
    config = bat_api.get_config()
    config["detection_threshold"] = threshold
    print("[BAT] Model loaded successfully")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )

    print(f"[BAT] Monitoring started - threshold: {threshold}, segment: {segment_duration}s")

    segment_count = 0

    while True:
        try:
            segment_count += 1

            # Capture audio segment
            audio_path = await capture.capture_segment(duration=segment_duration)

            # Run BatDetect2
            results = bat_api.process_file(audio_path, config=config)

            pred_dict = results.get("pred_dict", {})
            detections = pred_dict.get("annotation", [])
            sync_id = str(uuid.uuid4())
            detection_time = datetime.utcnow()

            if detections:
                print(f"[BAT] #{segment_count} | {len(detections)} bat call(s) detected!")

                # Optionally save audio for dashboard playback
                audio_saved_path = None
                if UPLOAD_BAT_AUDIO:
                    os.makedirs(BAT_AUDIO_DIR, exist_ok=True)
                    audio_saved_path = f"{BAT_AUDIO_DIR}/{sync_id}.wav"
                    shutil.copy2(audio_path, audio_saved_path)
                    print(f"  -> Audio saved to {audio_saved_path}")

                rows = []
                for det in detections:
                    species = det.get("class", "Unknown")
                    common_name = species  # BatDetect2 uses Latin names
                    det_prob = det.get("det_prob", 0.0)
                    start = det.get("start_time", 0.0)
                    end = det.get("end_time", 0.0)
                    low_freq = det.get("low_freq", 0.0)
                    high_freq = det.get("high_freq", 0.0)
                    duration_ms = (end - start) * 1000

                    print(f"  -> {species} (prob: {det_prob:.3f}, "
                          f"freq: {low_freq/1000:.1f}-{high_freq/1000:.1f} kHz, "
                          f"dur: {duration_ms:.1f} ms)")

                    rows.append((
                        species, species, det_prob,
                        start, end, low_freq, high_freq, duration_ms,
                        device_name, sync_id, detection_time, audio_saved_path
                    ))

                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO bat_detections
                        (species, common_name, detection_prob, start_time, end_time,
                         low_freq, high_freq, duration_ms, device, sync_id,
                         detection_time, audio_path)
                        VALUES %s
                    """, rows)
                conn.commit()
            else:
                if segment_count % 10 == 0:
                    print(f"[BAT] #{segment_count} | No bat calls detected (listening...)")

            # Clean up temp file
            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"[BAT] Error in segment #{segment_count}: {e}")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO capture_errors (service, error_type, message) "
                        "VALUES (%s, %s, %s)",
                        ("batdetect-service", type(e).__name__, str(e)[:500]),
                    )
                conn.commit()
            except Exception:
                pass
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
