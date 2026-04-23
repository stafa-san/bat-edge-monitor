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

    async def capture_segment(self, duration: int = 5):
        """Capture audio. Returns ``(wav_path, tempdir_handle)``.

        The caller **must** call ``tempdir_handle.cleanup()`` when it's
        done with the WAV, otherwise the file lingers in /tmp. Each call
        returns an independent TemporaryDirectory so segments don't stomp
        on each other — important now that capture runs concurrently
        with detection (producer/consumer pattern).

        Uses ``asyncio.create_subprocess_exec`` instead of the previous
        blocking ``subprocess.check_call`` so that during the 15-second
        arecord call the asyncio event loop is free to run the detection
        consumer on the previous segment. That's what recovers the
        ~45 % capture dead time noted in PIPELINE_AUDIT_AND_FIXES.md.
        """
        tmp = TemporaryDirectory()
        temp_file = f'{tmp.name}/bat_audio.wav'
        lock_fd = open(LOCK_PATH, 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            proc = await asyncio.create_subprocess_exec(
                'arecord', '-d', str(duration), '-D', self.device,
                '-f', 'S16_LE', '-r', str(self.sampling_rate),
                '-c', '1', '-q', temp_file,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                tmp.cleanup()
                raise subprocess.CalledProcessError(
                    proc.returncode, 'arecord',
                    stderr=(stderr or b'').decode('utf-8', 'replace'),
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        return temp_file, tmp


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

    # Diagnostic save — write a WAV for any segment where BatDetect2
    # passed its threshold but a downstream gate (classifier / validator /
    # FM-sweep) rejected it. Forensic data so we can inspect "near miss"
    # rejections manually and tell whether filter thresholds are right.
    diagnostic_save = os.getenv("DIAGNOSTIC_SAVE_REJECTIONS", "false").lower() == "true"
    diagnostic_dir = os.path.join(BAT_AUDIO_DIR, "_diagnostic")
    if diagnostic_save:
        os.makedirs(diagnostic_dir, exist_ok=True)
        print(f"[BAT] Diagnostic save enabled — near-miss rejections written to {diagnostic_dir}")

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

    # Warm-up forward pass. Forces BatDetect2 to fully initialise its
    # lazy weights + any torch JIT state before the capture loop starts
    # taking real segments. See BATDETECT2_STABILITY_FIX.md for the
    # Cloud-Function incident where the detector silently returned
    # zero raw detections for hours on audio that had just worked.
    #
    # Test signal: a burst of 5 downward FM chirps (60 kHz → 25 kHz
    # over 6 ms each) spread across a 1-second window. A pure sine
    # tone DOES NOT trigger BatDetect2 — the detector is trained on
    # FM sweeps, not carriers, so it correctly ignores steady tones.
    # The FM chirp shape is what a real bat call looks like; a healthy
    # model MUST emit detections on this input. If it returns zero,
    # the detector is in a degenerate state and we crash so Docker
    # recycles us — better a loud restart loop than weeks of silent
    # "no bat calls" in the field.
    try:
        import torch
        from scipy.signal import chirp as _scipy_chirp
        # Pin torch RNG + thread count. Pi 5 has 4 cores; give all of
        # them to BatDetect2 (unlike the CF where we pinned to 1 due
        # to shared-vCPU contention).
        torch.manual_seed(0)
        torch.set_num_threads(max(1, os.cpu_count() or 4))
        _wu_sr = int(config.get("target_samp_rate", 256000))
        _wu_audio = np.zeros(_wu_sr, dtype=np.float32)
        # 5 chirps spaced evenly across 1 s. Each is a 6 ms downward
        # FM sweep from 60 kHz to 25 kHz — bread-and-butter shape for
        # most NA+UK microbats in BatDetect2's training distribution.
        _wu_chirp_dur = 0.006
        _wu_chirp_n = int(_wu_sr * _wu_chirp_dur)
        _wu_chirp_t = np.linspace(0.0, _wu_chirp_dur, _wu_chirp_n, endpoint=False)
        _wu_chirp = _scipy_chirp(
            _wu_chirp_t, f0=60_000, f1=25_000,
            t1=_wu_chirp_dur, method="linear",
        ).astype(np.float32)
        for _wu_i in range(5):
            _wu_start = int((0.1 + 0.15 * _wu_i) * _wu_sr)
            _wu_audio[_wu_start:_wu_start + _wu_chirp_n] += 0.5 * _wu_chirp
        # Thin ambient noise so the spectrogram has texture.
        _wu_audio += 0.005 * np.random.randn(_wu_sr).astype(np.float32)
        # Use a permissive threshold for the warm-up — we care whether
        # the detector can see the chirps at all, not about tuning.
        _wu_cfg = dict(config)
        _wu_cfg["detection_threshold"] = 0.1
        _wu_dets, _, _ = bat_api.process_audio(_wu_audio, config=_wu_cfg)
        print(f"[BAT] BatDetect2 warm-up complete (raw_dets={len(_wu_dets)})")
        if len(_wu_dets) == 0:
            raise RuntimeError(
                "BatDetect2 warm-up saw 0 detections on 5× synthetic "
                "60→25 kHz FM chirps at threshold 0.1 — model is in a "
                "degenerate state. Crashing so Docker restarts the container."
            )
    except Exception as exc:
        print(f"[BAT] BatDetect2 warm-up FAILED: {exc}")
        raise

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

    # Model-health watchdog — tracks consecutive segments where the
    # detector returned raw_count=0 AND the audio clearly wasn't
    # silent. See BATDETECT2_STABILITY_FIX.md for the CF-nondeterminism
    # backstory. Log-only for now (no auto-restart until we've seen
    # this trigger under real field conditions).
    _HEALTH_BAD_THRESHOLD = 20
    _HEALTH_MIN_RMS = 2.0 * validator_cfg.get("min_rms", 0.002)

    # Capture queue: producer feeds, consumer drains. maxsize=3 means
    # if detection temporarily runs slower than capture (~12 s vs 15 s
    # in normal state), we buffer up to 45 s of audio before the
    # producer blocks. If the producer does block, we lose capture
    # duty-cycle but only during that backlog — much better than the
    # old serial loop which had ~45 % permanent dead time because
    # arecord only ran between processing passes (see
    # PIPELINE_AUDIT_AND_FIXES.md).
    segment_queue: asyncio.Queue = asyncio.Queue(maxsize=3)
    # Shared counters across producer/consumer — mutable holders so
    # the closures can modify without ``nonlocal`` gymnastics.
    segment_counter = {"n": 0}
    health_state = {"consecutive_bad": 0}

    async def capture_producer():
        """Continuously record 15 s WAV segments and queue them for analysis.

        Runs as its own asyncio task so arecord's 15-second wall-clock
        doesn't stall the detection consumer. Each queued item carries
        its own TemporaryDirectory handle — the consumer is responsible
        for ``cleanup()`` after processing.
        """
        while True:
            try:
                # Disk-watchdog kill switch — sync-service touches the
                # flag when the SD card is full; we wait (no capture).
                if HALT_FLAG.exists():
                    print("[BAT] Recordings halted by disk watchdog; waiting...")
                    await asyncio.sleep(60)
                    continue
                wav_path, tmp_dir = await capture.capture_segment(
                    duration=segment_duration
                )
                await segment_queue.put((wav_path, tmp_dir))
            except Exception as exc:  # noqa: BLE001 — keep producer alive
                print(f"[BAT] capture_producer error: {exc}")
                await asyncio.sleep(2)

    async def detect_consumer():
        """Drain captured segments through the full detection pipeline."""
        nonlocal conn
        while True:
            wav_path, tmp_dir = await segment_queue.get()
            try:
                segment_counter["n"] += 1
                segment_count = segment_counter["n"]

                rms, peak = _compute_audio_stats(wav_path)

                rejection_reason = None
                bd_stats = None
                if enable_classifier:
                    rows_data, rejection_reason, bd_stats = _run_batdetect_with_classifier(
                        wav_path, classifier_model, classifier_ckpt, config,
                        hpf_sos=hpf_sos, min_pred_conf=min_pred_conf,
                        validator_cfg=validator_cfg,
                        fm_sweep_cfg=fm_sweep_cfg,
                        user_threshold=threshold,
                    )
                else:
                    rows_data = _run_batdetect_legacy(
                        wav_path, config, hpf_sos=hpf_sos
                    )

                # Model-health watchdog — "real audio but detector saw
                # literally nothing" is the silent-failure signature.
                _raw_count = (bd_stats or {}).get("raw_count") or 0
                if rms is not None and rms > _HEALTH_MIN_RMS and _raw_count == 0:
                    health_state["consecutive_bad"] += 1
                    bad_n = health_state["consecutive_bad"]
                    if bad_n == _HEALTH_BAD_THRESHOLD:
                        print(
                            f"[BAT] MODEL-HEALTH WARNING: {_HEALTH_BAD_THRESHOLD} "
                            f"consecutive segments with rms>{_HEALTH_MIN_RMS:.4f} "
                            f"and raw_count=0 — detector may be in a degenerate "
                            f"state. See BATDETECT2_STABILITY_FIX.md. Consider "
                            f"`docker compose restart batdetect-service`."
                        )
                    elif bad_n > _HEALTH_BAD_THRESHOLD and \
                         bad_n % _HEALTH_BAD_THRESHOLD == 0:
                        print(
                            f"[BAT] MODEL-HEALTH WARNING: still bad "
                            f"({bad_n} consecutive segments)"
                        )
                else:
                    if health_state["consecutive_bad"] >= _HEALTH_BAD_THRESHOLD:
                        print(
                            f"[BAT] MODEL-HEALTH RECOVERED after "
                            f"{health_state['consecutive_bad']} bad segments"
                        )
                    health_state["consecutive_bad"] = 0

                # Diagnostic save of near-miss rejections (see
                # DIAGNOSTIC_SAVE_REJECTIONS comment in docker-compose.yml).
                if (
                    diagnostic_save
                    and rejection_reason is not None
                    and bd_stats
                    and (bd_stats.get("count_above_user") or 0) > 0
                ):
                    try:
                        os.makedirs(diagnostic_dir, exist_ok=True)
                        from datetime import datetime as _dt
                        ts = _dt.utcnow().strftime("%Y%m%dT%H%M%SZ")
                        safe_reason = (
                            rejection_reason
                            .replace("(", "_").replace(")", "").replace("=", "")
                            .replace(".", "p").replace("+", "").replace("/", "-")
                            .replace(":", "-").replace(",", "_")
                        )[:80]
                        diag_name = f"{site_id}_{ts}__BDpass_{safe_reason}.wav"
                        diag_dest = os.path.join(diagnostic_dir, diag_name)
                        shutil.copy2(wav_path, diag_dest)
                        print(
                            f"[BAT] DIAG saved: {diag_name} "
                            f"(bd_max={bd_stats.get('max_det_prob'):.3f}, "
                            f"user_pass={bd_stats.get('count_above_user')})"
                        )
                    except Exception as e:
                        print(f"[BAT] diagnostic save failed: {e}")

                await _handle_detection_result(
                    segment_count=segment_count,
                    wav_path=wav_path,
                    rms=rms, peak=peak,
                    rejection_reason=rejection_reason,
                    bd_stats=bd_stats,
                    rows_data=rows_data,
                )
            except Exception as exc:  # noqa: BLE001 — keep consumer alive
                print(f"[BAT] detect_consumer error (#{segment_counter['n']}): {exc}")
                try:
                    conn = ensure_connection(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO capture_errors (service, error_type, message) "
                            "VALUES (%s, %s, %s)",
                            ("batdetect-service", type(exc).__name__, str(exc)[:500]),
                        )
                    conn.commit()
                except Exception:
                    pass
                await asyncio.sleep(2)
            finally:
                # Always clean up the tempdir so /tmp doesn't fill up.
                try:
                    tmp_dir.cleanup()
                except Exception:
                    pass
                segment_queue.task_done()

    async def _handle_detection_result(*, segment_count, wav_path, rms, peak,
                                       rejection_reason, bd_stats, rows_data):
        """Everything after detection runs: DB insert, archive, log."""
        nonlocal conn

        # Audio-level + BD-stats + rejection sample for dashboard
        # troubleshooting. One row per captured segment — auto-expired
        # after 7 days by sync-service.
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

            # Decide where the WAV goes. Tiering supersedes
            # UPLOAD_BAT_AUDIO when enabled; otherwise fall back to the
            # legacy "copy if UPLOAD_BAT_AUDIO" behavior.
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
                        wav_path, tier, class_folder,
                        site_id, detection_time, BAT_AUDIO_DIR,
                    )
                audio_saved_path = str(archived_path) if archived_path else None
                file_storage_tier = tier
                folder_str = f"/{class_folder}" if class_folder else ""
                max_det_prob = max(
                    (d.get("det_prob", 0.0) for d, _ in rows_data), default=0.0,
                )
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
                shutil.copy2(wav_path, audio_saved_path)
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

    # Run producer + consumer concurrently. When the producer is in the
    # middle of a 15-second arecord call, the event loop yields control
    # back to the consumer to process the previous segment. Net effect:
    # capture duty cycle goes from ~55 % (serial loop) to ~100 %.
    await asyncio.gather(capture_producer(), detect_consumer())

if __name__ == "__main__":
    asyncio.run(main())
