import asyncio
import os
import re
import subprocess
import uuid
from datetime import datetime
from tempfile import TemporaryDirectory

import numpy as np
import psycopg2
from psycopg2.extras import execute_values
from batdetect2 import api as bat_api


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
        subprocess.check_call(command, shell=True)
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
            results = bat_api.process_file(audio_path, detection_threshold=threshold)

            detections = results.get("annotations", [])
            sync_id = str(uuid.uuid4())
            detection_time = datetime.utcnow()

            if detections:
                print(f"[BAT] #{segment_count} | {len(detections)} bat call(s) detected!")

                rows = []
                for det in detections:
                    species = det.get("class", "Unknown")
                    common_name = det.get("class_prob", {})
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
                        device_name, sync_id, detection_time
                    ))

                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO bat_detections
                        (species, common_name, detection_prob, start_time, end_time,
                         low_freq, high_freq, duration_ms, device, sync_id, detection_time)
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
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
