"""Tiered on-Pi storage for bat recordings.

After the groups classifier produces predictions for a captured WAV, this
module decides:

  * Which retention tier the file belongs in (1–3)
  * Which species subfolder to drop tier 1 into when a single file
    contains multiple predicted classes
  * Where on disk to write the archived copy (one-shot move, no
    temp-then-move)
  * When (if ever) the file should expire and be reclaimed by the disk
    watchdog in sync-service

The thresholds in this module are **tunable**. Dr. Johnson should edit
them here directly — no code hunting required. After any change, bump
``THRESHOLDS_LAST_TUNED`` so we can correlate training / evaluation runs
with whatever rules produced the data.

Tiers now require BOTH strong classifier confidence AND strong BatDetect2
base detection probability. This AND-gate is what keeps broadband fan /
wind noise from landing in the permanent archive even when the classifier
is forced to pick a closest-match NA class.

See ``DATA_FLYWHEEL.md`` (root of repo) for the broader architecture.
"""

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# -----------------------------------------------------------------------------
# TUNABLE TIER THRESHOLDS
# -----------------------------------------------------------------------------

THRESHOLDS_LAST_TUNED = "2026-04-20"

# Tier 1: permanent archive + gdrive upload
TIER1_CONFIDENCE_MIN = 0.7     # classifier prediction_confidence
TIER1_DET_PROB_MIN = 0.5       # BatDetect2 det_prob

# Tier 2: 30-day local retention
TIER2_CONFIDENCE_MIN = 0.4
TIER2_DET_PROB_MIN = 0.5

# No anomaly tier writes — tier 4 kept as a label in TIER_DIRS for
# backwards compatibility but determine_tier() never returns 4.
TIER4_CONFIDENCE_MAX = 0.0

CLASS_PRIORITY_ORDER: Tuple[str, ...] = ("PESU", "LACI", "LABO", "MYSP", "EPFU_LANO")
RARE_CLASSES = {"PESU", "LACI", "LABO", "MYSP", "EPFU_LANO"}
COMMON_CLASSES: set = set()

# Tier retention periods. Tier 1 is permanent (no expiry); tier 3 never
# writes a WAV, so it has no expiry either.
TIER2_RETENTION = timedelta(days=30)
TIER4_RETENTION = timedelta(days=7)

TIER_DIRS = {
    1: "tier1_permanent",
    2: "tier2_30day",
    4: "tier4_anomaly",
}


# -----------------------------------------------------------------------------
# Pure functions — no filesystem side effects
# -----------------------------------------------------------------------------

def determine_tier(rows_data: Iterable[Tuple[dict, Optional[dict]]]) -> int:
    """Pick a storage tier from (detection, prediction) tuples.

    Each tuple is ``(detection_dict, prediction_dict_or_None)`` where
    ``detection_dict`` has a ``det_prob`` key and ``prediction_dict`` (when
    present) has ``predicted_class`` and ``prediction_confidence``.

    Tier logic is AND-gated on both scores:
      * tier 1: any detection with prediction_confidence >= 0.7 AND
        det_prob >= 0.5
      * tier 2: any detection with prediction_confidence >= 0.4 AND
        det_prob >= 0.5
      * tier 3: metadata only (no WAV written)
    """
    rows = list(rows_data)
    if not rows:
        return 3

    def _qualifies(conf_min: float, det_min: float) -> bool:
        for det, pred in rows:
            if pred is None:
                continue
            if (
                pred["prediction_confidence"] >= conf_min
                and det.get("det_prob", 0.0) >= det_min
            ):
                return True
        return False

    if _qualifies(TIER1_CONFIDENCE_MIN, TIER1_DET_PROB_MIN):
        return 1
    if _qualifies(TIER2_CONFIDENCE_MIN, TIER2_DET_PROB_MIN):
        return 2
    return 3


def pick_class_folder(
    tier: int,
    rows_data: Iterable[Tuple[dict, Optional[dict]]],
) -> Optional[str]:
    """Choose the species subfolder for a tier-1 file.

    When a single WAV contains multiple predicted classes we route it to
    the rarest one present (per ``CLASS_PRIORITY_ORDER``) so Dr. Johnson's
    review workflow surfaces rare species first.

    Returns ``None`` for tiers 2/3/4 (they share a flat directory).
    """
    if tier != 1:
        return None

    classes_present = {
        pred["predicted_class"]
        for _, pred in rows_data
        if pred is not None
    }
    for cls in CLASS_PRIORITY_ORDER:
        if cls in classes_present:
            return cls
    # Unknown class — shouldn't happen if the classifier is in its
    # trained domain, but degrade gracefully rather than crash.
    return None


def compute_expires_at(tier: int, now: Optional[datetime] = None) -> Optional[datetime]:
    """Return when a file in this tier should be reclaimed.

    Tier 1 returns ``None`` (permanent). Tier 3 also returns ``None``
    (no file is written, so nothing to expire).
    """
    if tier in (1, 3):
        return None
    now = now or datetime.now(timezone.utc)
    if tier == 2:
        return now + TIER2_RETENTION
    if tier == 4:
        return now + TIER4_RETENTION
    raise ValueError(f"unknown tier: {tier!r}")


def build_filename(site_id: str, detection_time: datetime) -> str:
    """Filesystem-safe name for an archived WAV.

    Example: ``pi01_20260417T143022Z.wav``

    * UTC, ``T`` separator, no colons (works on FAT / NTFS / ext4)
    * ``Z`` suffix so the timestamp is unambiguously UTC
    """
    # Normalize to UTC without touching naive datetimes.
    if detection_time.tzinfo is None:
        ts = detection_time.replace(tzinfo=timezone.utc)
    else:
        ts = detection_time.astimezone(timezone.utc)
    ts_str = ts.strftime("%Y%m%dT%H%M%SZ")
    return f"{site_id}_{ts_str}.wav"


# -----------------------------------------------------------------------------
# Filesystem side effects
# -----------------------------------------------------------------------------

def archive_wav(
    wav_src_path: str,
    tier: int,
    class_folder: Optional[str],
    site_id: str,
    detection_time: datetime,
    bat_audio_dir: str,
) -> Tuple[Optional[Path], Optional[datetime]]:
    """Move a captured WAV into its tier directory. One-shot, no temp copy.

    Returns ``(destination_path, expires_at)``. Tier 3 skips the write
    entirely and returns ``(None, None)`` — the caller should still record
    the detection metadata in Postgres but with ``audio_path = NULL``.
    """
    if tier == 3:
        return (None, None)

    tier_dir_name = TIER_DIRS.get(tier)
    if tier_dir_name is None:
        raise ValueError(f"cannot archive WAV for tier {tier!r}")

    base = Path(bat_audio_dir) / tier_dir_name
    if tier == 1 and class_folder:
        base = base / class_folder
    base.mkdir(parents=True, exist_ok=True)

    filename = build_filename(site_id, detection_time)
    dest = base / filename
    shutil.move(wav_src_path, dest)
    return (dest, compute_expires_at(tier))
