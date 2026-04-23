"""Shared 4-gate bat-analysis pipeline.

Single source of truth for what "analyze this audio for bats" means in
this project. The Pi's live ``batdetect-service`` and the cloud upload
worker both call ``run_full_pipeline`` so a WAV captured at the mic and
a WAV uploaded from the dashboard traverse the exact same gates.

Gates, in order:

1. **High-pass filter (HPF) at 16 kHz** — defensive; bat calls are
   >20 kHz so anything below is not a bat. Applied to the in-memory
   audio only; callers keep the unfiltered WAV for archival.

2. **BatDetect2** at a permissive diagnostic threshold so we can log
   sub-user emissions for troubleshooting. A second mask then enforces
   the user / training threshold (``det_prob ≥ 0.5``).

3. **Groups classifier head** over the 32-dim features BatDetect2 emits.
   Rejects predictions whose softmax max falls below ``min_pred_conf``.

4. **Per-detection shape filter** (FM-sweep slope, low-band ratio,
   peak-frequency R²) — rejects broadband clicks that slipped through
   the detector.

5. **Segment-level audio validator** (RMS, bat-band SNR, burst ratio) —
   last-mile guard against "classifier confidently mis-labels silence"
   failures. Runs last because it's cheap and only fires when something
   already passed the earlier gates.

The return value carries ``rejection_reason`` even on zero-detection
results so upload-driven UIs can tell the user *why* nothing was
identified instead of showing a blank "0 detections" card.

Pipeline changes bump ``PIPELINE_VERSION``. Every detection row written
carries the version so Pi and Cloud rows can be compared across deploys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from batdetect2 import api as bat_api
from scipy.signal import butter, sosfiltfilt

from src.audio_validator import has_bat_call_shape, is_likely_bat_call
from src.classifier import classify

PIPELINE_VERSION = "v1-2026-04-22"

# BatDetect2 is queried at this permissive threshold so we can observe
# sub-user-threshold emissions for troubleshooting. The user-facing
# threshold is enforced by ``user_threshold`` / ``CLASSIFIER_DET_THRESHOLD``
# downstream, so behaviour is unchanged.
DIAGNOSTIC_BD_THRESHOLD = 0.1

# Matches training: BatDetect2 features were filtered at det_prob > 0.5
# when the classifier head was trained. Inference uses the same
# threshold to keep classifier inputs in the training distribution.
CLASSIFIER_DET_THRESHOLD = 0.5


# -----------------------------------------------------------------------------
# HPF design — cached by (cutoff, rate, order).
# -----------------------------------------------------------------------------

_hpf_cache: Dict[Tuple[float, int, int], np.ndarray] = {}


def _get_hpf_sos(cutoff_hz: float, sample_rate: int, order: int) -> np.ndarray:
    key = (float(cutoff_hz), int(sample_rate), int(order))
    if key not in _hpf_cache:
        _hpf_cache[key] = butter(
            order, cutoff_hz, btype="highpass", fs=sample_rate, output="sos"
        )
    return _hpf_cache[key]


def _apply_hpf(audio: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """Zero-phase Butterworth HPF. Preserves length and dtype."""
    return sosfiltfilt(sos, audio).astype(audio.dtype, copy=False)


# -----------------------------------------------------------------------------
# Result type
# -----------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Everything a caller needs to persist + render a single analysis."""

    # Surviving (detection, prediction) pairs. Empty when a gate rejected.
    detections: List[Tuple[dict, dict]] = field(default_factory=list)

    # None on success; otherwise a machine-parseable code naming the
    # gate + metric that caused rejection. Always present when detections
    # is empty so callers never have to say "0 detections, cause unknown".
    rejection_reason: Optional[str] = None

    # Diagnostic counters for logging — always populated. Stable keys:
    #   raw_count          int  — total BatDetect2 detections at diagnostic threshold
    #   max_det_prob       float — highest det_prob this segment
    #   count_above_user   int  — detections ≥ user_threshold (pre-classifier)
    #   top_class          Optional[str] — raw BatDetect2 top-class label
    stats: Dict[str, Any] = field(default_factory=dict)

    # Audio duration as loaded by BatDetect2 (seconds).
    duration_seconds: float = 0.0

    # Identifier for the pipeline that produced this result. Rows written
    # to Postgres / Firestore should copy this value into their
    # ``pipeline_version`` field so deploy drift is observable.
    pipeline_version: str = PIPELINE_VERSION


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def run_full_pipeline(
    wav_path: str,
    classifier_model,
    classifier_ckpt,
    *,
    bd_config: Optional[dict] = None,
    user_threshold: float = 0.5,
    min_pred_conf: float = 0.6,
    hpf_enabled: bool = True,
    hpf_cutoff_hz: float = 16000.0,
    hpf_order: int = 4,
    validator_enabled: bool = True,
    validator_min_rms: float = 0.005,
    validator_min_snr_db: float = 10.0,
    validator_min_burst_ratio: float = 3.0,
    fm_sweep_enabled: bool = True,
    fm_sweep_min_slope: float = -0.1,
    fm_sweep_max_low_band_ratio: float = 0.5,
    fm_sweep_min_r2: float = 0.2,
) -> PipelineResult:
    """Run the full 4-gate analysis on a WAV file.

    Parameters match the env-var knobs ``batdetect-service`` reads at
    startup, so a Pi capture with default Docker env and a cloud-worker
    upload with default pipeline kwargs produce identical output on the
    same file.

    ``classifier_model`` / ``classifier_ckpt`` come from
    ``classifier.load_groups_classifier(model_path)``. Callers are
    responsible for caching them across calls.
    """
    stats: Dict[str, Any] = {
        "raw_count": 0,
        "max_det_prob": 0.0,
        "count_above_user": 0,
        "top_class": None,
    }

    if bd_config is None:
        bd_config = bat_api.get_config()

    # ── Load + HPF ─────────────────────────────────────────────────
    audio = bat_api.load_audio(wav_path)
    target_sr = int(bd_config.get("target_samp_rate", 256000))
    duration_s = float(len(audio)) / float(target_sr) if target_sr else 0.0

    if hpf_enabled:
        hpf_sos = _get_hpf_sos(hpf_cutoff_hz, target_sr, hpf_order)
        audio = _apply_hpf(audio, hpf_sos)

    # ── Gate 1 — BatDetect2 ────────────────────────────────────────
    diag_config = dict(bd_config)
    diag_config["detection_threshold"] = min(
        DIAGNOSTIC_BD_THRESHOLD, user_threshold
    )
    detections, features, _ = bat_api.process_audio(audio, config=diag_config)

    if detections:
        probs = [d.get("det_prob", 0.0) for d in detections]
        stats["raw_count"] = len(detections)
        stats["max_det_prob"] = float(max(probs))
        stats["count_above_user"] = sum(1 for p in probs if p >= user_threshold)
        top = max(detections, key=lambda d: d.get("det_prob", 0.0))
        stats["top_class"] = top.get("class")

    if not detections:
        return PipelineResult(
            rejection_reason="batdetect2_no_detections",
            stats=stats,
            duration_seconds=duration_s,
        )

    # User-threshold gate — the real threshold matching training.
    threshold = max(user_threshold, CLASSIFIER_DET_THRESHOLD)
    mask = np.array([d.get("det_prob", 0.0) >= threshold for d in detections])
    if not mask.any():
        return PipelineResult(
            rejection_reason="all_below_user_threshold",
            stats=stats,
            duration_seconds=duration_s,
        )

    high_conf_dets = [d for d, m in zip(detections, mask) if m]
    high_conf_feats = features[mask]

    # ── Gate 2 — Classifier head ───────────────────────────────────
    preds = classify(high_conf_feats, classifier_model, classifier_ckpt)
    kept = [
        (d, p) for d, p in zip(high_conf_dets, preds)
        if p["prediction_confidence"] >= min_pred_conf
    ]
    if not kept:
        return PipelineResult(
            rejection_reason="all_below_min_pred_conf",
            stats=stats,
            duration_seconds=duration_s,
        )

    # ── Gate 3 — FM-sweep / low-band-ratio shape filter ────────────
    if fm_sweep_enabled:
        passed = []
        shape_rejections = []
        for det, pred in kept:
            ok, reason, _shape_stats = has_bat_call_shape(
                audio, target_sr,
                det.get("start_time", 0.0), det.get("end_time", 0.0),
                min_slope_khz_per_ms=fm_sweep_min_slope,
                max_low_band_ratio=fm_sweep_max_low_band_ratio,
                min_r2=fm_sweep_min_r2,
            )
            if ok:
                passed.append((det, pred))
            else:
                shape_rejections.append(reason)
        if not passed:
            reason = shape_rejections[0] if shape_rejections else "shape_all_rejected"
            return PipelineResult(
                rejection_reason=f"shape:{reason}",
                stats=stats,
                duration_seconds=duration_s,
            )
        kept = passed

    # ── Gate 4 — Segment-level audio validator ─────────────────────
    if validator_enabled:
        ok, reason = is_likely_bat_call(
            audio, target_sr,
            min_rms=validator_min_rms,
            min_snr_db=validator_min_snr_db,
            min_burst_ratio=validator_min_burst_ratio,
        )
        if not ok:
            return PipelineResult(
                rejection_reason=f"validator:{reason}",
                stats=stats,
                duration_seconds=duration_s,
            )

    return PipelineResult(
        detections=kept,
        rejection_reason=None,
        stats=stats,
        duration_seconds=duration_s,
    )


# -----------------------------------------------------------------------------
# Human-readable mapping for UIs. Kept here so Pi and Cloud surfaces
# display the same message for the same rejection code.
# -----------------------------------------------------------------------------

_REJECTION_MESSAGES = {
    "batdetect2_no_detections": "BatDetect2 found no echolocation signatures in this recording.",
    "all_below_user_threshold": "Detected signals, but none above the confidence threshold.",
    "all_below_min_pred_conf": "Signals found, but classifier confidence was below the keep-threshold — no bat species identified.",
}


def humanize_rejection(reason: Optional[str]) -> str:
    """Map a ``rejection_reason`` code to something you can show a user."""
    if reason is None:
        return ""
    if reason in _REJECTION_MESSAGES:
        return _REJECTION_MESSAGES[reason]
    if reason.startswith("shape:"):
        inner = reason.split(":", 1)[1]
        if inner.startswith("broadband_noise"):
            return "Detected signals, but all looked like broadband clicks rather than bat calls."
        if inner.startswith("chaotic_peaks"):
            return "Detected signals with erratic frequency patterns — not a downward-sweep bat call."
        if inner.startswith("not_downward_sweep"):
            return "Detected signals but none had the downward frequency sweep of a bat call."
        return "Detected signals, but their shape did not match a bat call."
    if reason.startswith("validator:"):
        inner = reason.split(":", 1)[1]
        if inner.startswith("rms_too_low"):
            return "Audio appears to be silence or very quiet — no bat calls."
        if inner.startswith("snr_too_low"):
            return "Audio is mostly broadband noise — no bat-call signature detected."
        if inner.startswith("no_burst"):
            return "Audio has no transient burst — likely steady-state noise, not an echolocation pass."
        return "Audio failed the bat-call sanity check."
    return "No bat species identified in this recording."
