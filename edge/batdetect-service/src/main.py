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
from scipy.io import wavfile
from scipy.signal import butter, sosfiltfilt

from src import storage
from src.audio_validator import has_bat_call_shape, is_likely_bat_call
from src.classifier import classify, load_groups_classifier


def _format_bd_stats(stats: dict | None) -> str:
    """Compact log tail describing what BatDetect2 emitted this segment.

    Lets the "No bat calls detected" heartbeat distinguish these three
    cases at a glance:
        bd_raw=0                              — detector saw nothing
        bd_raw=3 max=0.22                     — weak sub-user signal
        bd_raw=8 max=0.48 user_pass=0         — near-misses, tune down
    """
    if not stats or stats.get("raw_count", 0) == 0:
        return " (bd_raw=0)"
    parts = [f"bd_raw={stats['raw_count']}", f"max={stats['max_det_prob']:.2f}"]
    if "count_above_user" in stats:
        parts.append(f"user_pass={stats['count_above_user']}")
    top = stats.get("top_class")
    if top:
        parts.append(f"top={top[:18]}")
    return " (" + " ".join(parts) + ")"


def _compute_audio_stats(wav_path: str) -> tuple[float | None, float | None]:
    """Return ``(rms, peak)`` in 0..1 normalised amplitude, or ``(None, None)``.

    Runs on the raw captured WAV before any software processing — what
    the AudioMoth actually delivered to the USB bus. Used to surface
    mic health on the dashboard (silent / undervolted mic).
    """
    try:
        _sr, audio = wavfile.read(wav_path)
        if audio.ndim > 1:
            audio = audio[:, 0]
        if audio.dtype.kind == "i":
            max_val = float(np.iinfo(audio.dtype).max)
            audio_f = audio.astype(np.float32) / max_val
        else:
            audio_f = audio.astype(np.float32)
        rms = float(np.sqrt(np.mean(audio_f * audio_f)))
        peak = float(np.max(np.abs(audio_f)))
        return rms, peak
    except Exception:
        return None, None


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

# Software high-pass filter applied to the in-memory audio *before* it
# hits BatDetect2. The AudioMoth hardware HPF at 8 kHz is the primary
# cut; this is a secondary defence at 16 kHz to keep any residual
# low-frequency energy (fan, wind, electrical) out of the detector's
# view. The archived WAV remains unfiltered so advisors can inspect it
# with full context.
HPF_ENABLED = os.getenv("HPF_ENABLED", "true").lower() == "true"
HPF_CUTOFF_HZ = float(os.getenv("HPF_CUTOFF_HZ", "16000"))
HPF_ORDER = int(os.getenv("HPF_ORDER", "4"))

# Shared control volume with sync-service. Disk watchdog touches this file
# when it wants us to stop capturing until pressure is relieved.
HALT_FLAG = Path("/control/halt_recordings")


def _design_hpf(cutoff_hz: float, sample_rate: int, order: int):
    """Return a SOS Butterworth HPF design for the given sample rate."""
    return butter(order, cutoff_hz, btype="highpass", fs=sample_rate, output="sos")


def _apply_hpf(audio: np.ndarray, sos) -> np.ndarray:
    """Zero-phase Butterworth HPF. Preserves length and dtype-compatible output."""
    return sosfiltfilt(sos, audio).astype(audio.dtype, copy=False)


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


def _run_batdetect_legacy(audio_path, config, hpf_sos=None):
    """Legacy path — raw BatDetect2 only. Used when the classifier is disabled.

    When ``hpf_sos`` is provided, the audio is loaded, high-pass filtered,
    and fed to ``process_audio`` directly (bypassing ``process_file``) so
    the HPF actually takes effect.
    """
    if hpf_sos is None:
        results = bat_api.process_file(audio_path, config=config)
        pred_dict = results.get("pred_dict", {})
        detections = pred_dict.get("annotation", [])
        return [(d, None) for d in detections]

    audio = bat_api.load_audio(audio_path)
    audio = _apply_hpf(audio, hpf_sos)
    detections, _, _ = bat_api.process_audio(audio, config=config)
    return [(d, None) for d in detections]


DIAGNOSTIC_BD_THRESHOLD = 0.1  # let sub-threshold emissions through for logging


def _run_batdetect_with_classifier(
    audio_path, classifier_model, classifier_ckpt, config, hpf_sos=None,
    min_pred_conf: float = 0.6,
    validator_cfg: dict | None = None,
    fm_sweep_cfg: dict | None = None,
    user_threshold: float = 0.5,
):
    """Returns ``(rows_data, rejection_reason, stats)``.

    ``rows_data`` is the list of surviving ``(detection, prediction)``
    tuples that will become Postgres rows. ``rejection_reason`` names
    the gate that dropped the segment when ``rows_data`` is empty.
    ``stats`` is always populated and carries per-segment diagnostic
    counters so the caller can log what BatDetect2 saw even when
    nothing passed the pipeline. Keys:
        raw_count           — total detections above DIAGNOSTIC_BD_THRESHOLD
        max_det_prob        — highest det_prob seen this segment (0.0 if none)
        count_above_user    — detections >= user_threshold (pre-classifier)
        top_class           — BD's top class name (UK label), or None
    """
    stats = {
        "raw_count": 0,
        "max_det_prob": 0.0,
        "count_above_user": 0,
        "top_class": None,
    }

    audio = bat_api.load_audio(audio_path)
    if hpf_sos is not None:
        audio = _apply_hpf(audio, hpf_sos)
    # Query BatDetect2 at a permissive diagnostic threshold so we can
    # observe sub-user-threshold emissions for troubleshooting. The
    # user-facing DETECTION_THRESHOLD is enforced below in the mask,
    # so downstream pipeline behaviour is unchanged.
    diag_config = dict(config)
    diag_config["detection_threshold"] = min(DIAGNOSTIC_BD_THRESHOLD, user_threshold)
    detections, features, _ = bat_api.process_audio(audio, config=diag_config)

    if detections:
        probs = [d.get("det_prob", 0.0) for d in detections]
        stats["raw_count"] = len(detections)
        stats["max_det_prob"] = float(max(probs))
        stats["count_above_user"] = sum(1 for p in probs if p >= user_threshold)
        top = max(detections, key=lambda d: d.get("det_prob", 0.0))
        stats["top_class"] = top.get("class")

    if not detections:
        return [], "batdetect2_no_detections", stats

    # User-threshold gate. Previous versions force-floored this at
    # CLASSIFIER_DET_THRESHOLD (the training-distribution value, 0.5)
    # via max(). Lowered 2026-04-23 after the 005517.wav false-negative
    # experiment: UK-trained BatDetect2 is under-confident on NA bats,
    # and 0.5 was dropping real passes. Downstream gates — classifier
    # min_pred_conf 0.6 + FM-sweep shape filter + audio-level validator
    # — absorb the extra out-of-distribution noise. See
    # DETECTION_TUNING_PLAYBOOK.md for the full rationale.
    threshold = user_threshold
    mask = np.array([d.get("det_prob", 0.0) >= threshold for d in detections])
    if not mask.any():
        return [], "all_below_user_threshold", stats

    high_conf_dets = [d for d, m in zip(detections, mask) if m]
    high_conf_feats = features[mask]
    preds = classify(high_conf_feats, classifier_model, classifier_ckpt)

    # Classifier-confidence gate.
    kept = [
        (d, p) for d, p in zip(high_conf_dets, preds)
        if p["prediction_confidence"] >= min_pred_conf
    ]
    if not kept:
        return [], "all_below_min_pred_conf", stats

    # FM-sweep + low-band-ratio per-detection shape filter. Rejects
    # detections whose audio inside the bounding box looks like a
    # broadband click (insect, rain, mechanical) rather than a real
    # downward FM echolocation pulse.
    if fm_sweep_cfg and fm_sweep_cfg.get("enabled", True):
        passed = []
        shape_rejections = []
        for det, pred in kept:
            ok, reason, shape_stats = has_bat_call_shape(
                audio, int(config.get("target_samp_rate", 256000)),
                det.get("start_time", 0.0), det.get("end_time", 0.0),
                min_slope_khz_per_ms=fm_sweep_cfg.get("min_slope_khz_per_ms", -0.1),
                max_low_band_ratio=fm_sweep_cfg.get("max_low_band_ratio", 0.5),
                min_r2=fm_sweep_cfg.get("min_r2", 0.2),
            )
            if ok:
                passed.append((det, pred))
            else:
                shape_rejections.append(reason)
        if not passed:
            reason = shape_rejections[0] if shape_rejections else "shape_all_rejected"
            return [], f"shape:{reason}", stats
        kept = passed

    # Audio-level validator — "is this actually a bat call?"
    # Runs only when the classifier has at least one confident pick
    # so we don't waste cycles on silence. Rate for the spectrogram
    # checks comes from BatDetect2's target rate (its load_audio
    # resamples to target_samp_rate).
    if validator_cfg and validator_cfg.get("enabled", True):
        val_sr = int(config.get("target_samp_rate", 256000))
        ok, reason = is_likely_bat_call(
            audio, val_sr,
            min_rms=validator_cfg.get("min_rms", 0.005),
            min_snr_db=validator_cfg.get("min_snr_db", 10.0),
            min_burst_ratio=validator_cfg.get("min_burst_ratio", 3.0),
        )
        if not ok:
            return [], f"validator:{reason}", stats

    return kept, None, stats


async def main():
    device_name = os.getenv("DEVICE_NAME", "AudioMoth")
    sample_rate = int(os.getenv("SAMPLE_RATE", "192000"))
    threshold = float(os.getenv("DETECTION_THRESHOLD", "0.3"))
    min_pred_conf = float(os.getenv("MIN_PREDICTION_CONF", "0.6"))
    segment_duration = int(os.getenv("SEGMENT_DURATION", "5"))
    enable_classifier = os.getenv("ENABLE_GROUPS_CLASSIFIER", "false").lower() == "true"
    enable_storage_tiering = os.getenv("ENABLE_STORAGE_TIERING", "false").lower() == "true"
    site_id = os.getenv("PI_SITE", "pi01")
    model_path = os.getenv("MODEL_PATH", "/app/models/groups_model.pt")
    model_version = os.getenv("MODEL_VERSION", "groups_v1_post_epfu_partial_2026-04-17")

    # Audio-level validator (signal-processing sanity check, no ML).
    validator_cfg = {
        "enabled": os.getenv("VALIDATOR_ENABLED", "true").lower() == "true",
        "min_rms": float(os.getenv("VALIDATOR_MIN_RMS", "0.005")),
        "min_snr_db": float(os.getenv("VALIDATOR_MIN_SNR_DB", "10.0")),
        "min_burst_ratio": float(os.getenv("VALIDATOR_MIN_BURST_RATIO", "3.0")),
    }
    # Per-detection FM-sweep shape filter — 4th gate. Rejects detections
    # whose audio is broadband (insect clicks, rain) rather than real
    # downward FM echolocation.
    fm_sweep_cfg = {
        "enabled": os.getenv("FM_SWEEP_ENABLED", "true").lower() == "true",
        "min_slope_khz_per_ms": float(os.getenv("FM_SWEEP_MIN_SLOPE", "-0.1")),
        "max_low_band_ratio": float(os.getenv("FM_SWEEP_MAX_LOW_BAND_RATIO", "0.5")),
        "min_r2": float(os.getenv("FM_SWEEP_MIN_R2", "0.2")),
    }

    if enable_storage_tiering and not enable_classifier:
        raise RuntimeError(
            "ENABLE_STORAGE_TIERING requires ENABLE_GROUPS_CLASSIFIER=true — "
            "tier assignment reads the classifier's confidence per detection."
        )

    print(f"[BAT] Initializing audio capture: {device_name} @ {sample_rate} Hz")
    capture = BatAudioCapture(device_name=device_name, sampling_rate=sample_rate)

    hpf_sos = None
    if HPF_ENABLED:
        # BatDetect2 resamples to its internal target rate before analysis,
        # so design the filter against that rate to match what the detector
        # actually sees.
        hpf_design_rate = bat_api.get_config().get("target_samp_rate", sample_rate)
        hpf_sos = _design_hpf(HPF_CUTOFF_HZ, hpf_design_rate, HPF_ORDER)
        print(
            f"[BAT] HPF enabled: cutoff={int(HPF_CUTOFF_HZ)} Hz, "
            f"order={HPF_ORDER} (applied at {hpf_design_rate} Hz, "
            "analysis-only, archived WAV unchanged)"
        )
    else:
        print("[BAT] HPF disabled (HPF_ENABLED=false)")

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

    if validator_cfg["enabled"]:
        print(
            f"[BAT] Audio validator enabled — "
            f"min_rms={validator_cfg['min_rms']}, "
            f"min_snr_db={validator_cfg['min_snr_db']}, "
            f"min_burst_ratio={validator_cfg['min_burst_ratio']}"
        )
    else:
        print("[BAT] Audio validator disabled (VALIDATOR_ENABLED=false)")

    if fm_sweep_cfg["enabled"]:
        print(
            f"[BAT] FM-sweep shape filter enabled — "
            f"min_slope={fm_sweep_cfg['min_slope_khz_per_ms']} kHz/ms, "
            f"max_low_band_ratio={fm_sweep_cfg['max_low_band_ratio']}, "
            f"min_r2={fm_sweep_cfg['min_r2']}"
        )
    else:
        print("[BAT] FM-sweep shape filter disabled (FM_SWEEP_ENABLED=false)")

    conn = get_db_connection()

    print(
        f"[BAT] Monitoring started — batdetect_threshold={threshold}, "
        f"min_pred_conf={min_pred_conf}, segment={segment_duration}s"
    )

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

            # Compute audio stats first; we'll insert the audio_levels
            # row below after detection runs so we can include BD stats
            # and the rejection reason on the same row.
            rms, peak = _compute_audio_stats(audio_path)

            # Run detection (+ optionally classification)
            rejection_reason = None
            bd_stats = None
            if enable_classifier:
                rows_data, rejection_reason, bd_stats = _run_batdetect_with_classifier(
                    audio_path, classifier_model, classifier_ckpt, config,
                    hpf_sos=hpf_sos, min_pred_conf=min_pred_conf,
                    validator_cfg=validator_cfg,
                    fm_sweep_cfg=fm_sweep_cfg,
                    user_threshold=threshold,
                )
            else:
                rows_data = _run_batdetect_legacy(audio_path, config, hpf_sos=hpf_sos)

            # Audio-level + BD-stats + rejection sample for dashboard
            # troubleshooting. One row per captured segment — auto-
            # expired after 7 days by sync-service.
            if rms is not None:
                try:
                    conn = ensure_connection(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO audio_levels "
                            "(rms, peak, bd_raw_count, bd_max_det_prob, "
                            " bd_user_pass, rejection_reason) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (
                                rms, peak,
                                (bd_stats or {}).get("raw_count"),
                                (bd_stats or {}).get("max_det_prob"),
                                (bd_stats or {}).get("count_above_user"),
                                rejection_reason,
                            ),
                        )
                    conn.commit()
                except Exception as e:
                    # Non-fatal: don't let telemetry kill capture.
                    print(f"[BAT] audio_levels write failed: {e}")

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
                    tier = storage.determine_tier(rows_data)
                    class_folder = storage.pick_class_folder(tier, rows_data)
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
                    max_det_prob = max((d.get("det_prob", 0.0) for d, _ in rows_data), default=0.0)
                    max_pred_conf = max(
                        (p["prediction_confidence"] for _, p in rows_data if p is not None),
                        default=0.0,
                    )
                    print(
                        f"  -> tier {tier}{folder_str} "
                        f"(max det_prob={max_det_prob:.3f}, "
                        f"max pred_conf={max_pred_conf:.3f}) "
                        f"-> {audio_saved_path or '(no audio written)'}"
                    )
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
                # Validator rejections are logged every time so we can
                # see what's being filtered; pure no-detection heartbeats
                # stay at 1-per-10-segments to keep the log quiet.
                if rejection_reason and (
                    rejection_reason.startswith("validator:")
                    or rejection_reason.startswith("shape:")
                ):
                    bd_tail = _format_bd_stats(bd_stats)
                    print(f"[BAT] #{segment_count} | rejected by {rejection_reason}{bd_tail}")
                elif segment_count % 10 == 0:
                    # Listening heartbeat: include BD diagnostic stats so
                    # "detector saw nothing" is distinguishable from
                    # "detector saw weak sub-threshold signal."
                    bd_tail = _format_bd_stats(bd_stats)
                    print(f"[BAT] #{segment_count} | No bat calls detected{bd_tail}")

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
