import asyncio
import fcntl
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import psycopg2
from psycopg2.extras import execute_values
from batdetect2 import api as bat_api

from src import storage
from src.classifier import classify, load_groups_classifier


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
        print("[BAT] DB connection lost — reconnecting")
        try:
            conn.close()
        except Exception:
            pass
        return get_db_connection()

LOCK_PATH = "/locks/audio_device.lock"
UPLOAD_BAT_AUDIO = os.getenv("UPLOAD_BAT_AUDIO", "false").lower() == "true"
BAT_AUDIO_DIR = "/bat_audio"
CLASSIFIER_DET_THRESHOLD = float(os.getenv("CLASSIFIER_DET_THRESHOLD", "0.3"))

# Shared control volume with sync-service. Disk watchdog touches this file
# when it wants us to stop capturing until pressure is relieved.
HALT_FLAG = Path("/control/halt_recordings")


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


def _run_batdetect_legacy(audio_path, config):
    """Legacy path — raw BatDetect2 only. Used when the classifier is disabled."""
    results = bat_api.process_file(audio_path, config=config)
    pred_dict = results.get("pred_dict", {})
    detections = pred_dict.get("annotation", [])
    return [(d, None) for d in detections]


def _run_batdetect_with_classifier(audio_path, classifier_model, classifier_ckpt):
    """New path — process_audio gives us features for the classifier head.

    Returns a list of (detection_dict, prediction_dict_or_None) tuples,
    filtered to det_prob > CLASSIFIER_DET_THRESHOLD (matches training).
    """
    audio = bat_api.load_audio(audio_path)
    detections, features, _ = bat_api.process_audio(audio)
    if not detections:
        return []

    mask = np.array([d.get("det_prob", 0.0) > CLASSIFIER_DET_THRESHOLD for d in detections])
    if not mask.any():
        return []

    high_conf_dets = [d for d, m in zip(detections, mask) if m]
    high_conf_feats = features[mask]
    preds = classify(high_conf_feats, classifier_model, classifier_ckpt)
    return list(zip(high_conf_dets, preds))


async def main():
    device_name = os.getenv("DEVICE_NAME", "AudioMoth")
    sample_rate = int(os.getenv("SAMPLE_RATE", "192000"))
    threshold = float(os.getenv("DETECTION_THRESHOLD", "0.3"))
    segment_duration = int(os.getenv("SEGMENT_DURATION", "5"))
    enable_classifier = os.getenv("ENABLE_GROUPS_CLASSIFIER", "false").lower() == "true"
    enable_storage_tiering = os.getenv("ENABLE_STORAGE_TIERING", "false").lower() == "true"
    site_id = os.getenv("PI_SITE", "pi01")
    model_path = os.getenv("MODEL_PATH", "/app/models/groups_model.pt")
    model_version = os.getenv("MODEL_VERSION", "groups_v1_post_epfu_partial_2026-04-17")

    if enable_storage_tiering and not enable_classifier:
        raise RuntimeError(
            "ENABLE_STORAGE_TIERING requires ENABLE_GROUPS_CLASSIFIER=true — "
            "tier assignment reads the classifier's confidence per detection."
        )

    print(f"[BAT] Initializing audio capture: {device_name} @ {sample_rate} Hz")
    capture = BatAudioCapture(device_name=device_name, sampling_rate=sample_rate)

    print("[BAT] Loading BatDetect2 model...")
    config = bat_api.get_config()
    config["detection_threshold"] = threshold
    print("[BAT] BatDetect2 ready")

    classifier_model = None
    classifier_ckpt = None
    if enable_classifier:
        print(f"[BAT] Loading groups classifier from {model_path}")
        classifier_model, classifier_ckpt = load_groups_classifier(model_path)
        print(f"[BAT] Classifier ready: {classifier_ckpt['class_names']} "
              f"(model_version={model_version}, det_threshold={CLASSIFIER_DET_THRESHOLD})")
    else:
        print("[BAT] Groups classifier disabled (ENABLE_GROUPS_CLASSIFIER=false)")

    if enable_storage_tiering:
        print(f"[BAT] Storage tiering enabled — site_id={site_id}, bat_audio_dir={BAT_AUDIO_DIR}")
    else:
        print("[BAT] Storage tiering disabled (ENABLE_STORAGE_TIERING=false)")

    conn = get_db_connection()

    print(f"[BAT] Monitoring started — batdetect_threshold={threshold}, segment={segment_duration}s")

    segment_count = 0

    while True:
        try:
            # Disk-watchdog kill switch. Wait (don't capture) until sync-service
            # removes the flag.
            if HALT_FLAG.exists():
                if segment_count % 10 == 0:
                    print("[BAT] Recordings halted by disk watchdog; waiting...")
                await asyncio.sleep(60)
                continue

            segment_count += 1

            # Capture audio segment
            audio_path = await capture.capture_segment(duration=segment_duration)

            # Run detection (+ optionally classification)
            if enable_classifier:
                rows_data = _run_batdetect_with_classifier(
                    audio_path, classifier_model, classifier_ckpt,
                )
            else:
                rows_data = _run_batdetect_legacy(audio_path, config)

            sync_id = str(uuid.uuid4())
            detection_time = datetime.utcnow()

            if rows_data:
                print(f"[BAT] #{segment_count} | {len(rows_data)} bat call(s) detected!")

                # Decide where the WAV goes. Tiering supersedes UPLOAD_BAT_AUDIO
                # when enabled; otherwise fall back to the legacy "copy if
                # UPLOAD_BAT_AUDIO" behavior.
                audio_saved_path = None
                file_storage_tier = None
                file_expires_at = None

                if enable_storage_tiering:
                    predictions = [pred for _, pred in rows_data if pred is not None]
                    tier = storage.determine_tier(predictions)
                    class_folder = storage.pick_class_folder(tier, predictions)
                    if tier == 3:
                        archived_path = None
                        file_expires_at = None
                    else:
                        archived_path, file_expires_at = storage.archive_wav(
                            audio_path, tier, class_folder,
                            site_id, detection_time, BAT_AUDIO_DIR,
                        )
                    audio_saved_path = str(archived_path) if archived_path else None
                    file_storage_tier = tier
                    folder_str = f"/{class_folder}" if class_folder else ""
                    print(f"  -> tier {tier}{folder_str} -> {audio_saved_path or '(no audio written)'}")
                elif UPLOAD_BAT_AUDIO:
                    os.makedirs(BAT_AUDIO_DIR, exist_ok=True)
                    audio_saved_path = f"{BAT_AUDIO_DIR}/{sync_id}.wav"
                    shutil.copy2(audio_path, audio_saved_path)
                    print(f"  -> Audio saved to {audio_saved_path}")

                rows = []
                for det, pred in rows_data:
                    species = det.get("class", "Unknown")
                    common_name = species  # BatDetect2 uses Latin names
                    det_prob = det.get("det_prob", 0.0)
                    start = det.get("start_time", 0.0)
                    end = det.get("end_time", 0.0)
                    low_freq = det.get("low_freq", 0.0)
                    high_freq = det.get("high_freq", 0.0)
                    duration_ms = (end - start) * 1000

                    predicted_class = pred["predicted_class"] if pred else None
                    prediction_confidence = pred["prediction_confidence"] if pred else None
                    row_model_version = model_version if pred else None

                    log_tail = (
                        f" -> {predicted_class} ({prediction_confidence:.3f})"
                        if pred else ""
                    )
                    print(f"  -> {species} (prob: {det_prob:.3f}, "
                          f"freq: {low_freq/1000:.1f}-{high_freq/1000:.1f} kHz, "
                          f"dur: {duration_ms:.1f} ms){log_tail}")

                    rows.append((
                        species, common_name, det_prob,
                        start, end, low_freq, high_freq, duration_ms,
                        device_name, sync_id, detection_time, audio_saved_path,
                        predicted_class, prediction_confidence, row_model_version,
                        file_storage_tier, file_expires_at,
                    ))

                conn = ensure_connection(conn)
                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO bat_detections
                        (species, common_name, detection_prob, start_time, end_time,
                         low_freq, high_freq, duration_ms, device, sync_id,
                         detection_time, audio_path,
                         predicted_class, prediction_confidence, model_version,
                         storage_tier, expires_at)
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
            conn = ensure_connection(conn)
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
