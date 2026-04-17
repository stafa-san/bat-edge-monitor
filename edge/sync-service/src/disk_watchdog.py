"""Disk quota enforcement for the Pi's bat-audio store.

Runs every sync cycle from sync-service/main.py. Two jobs:

  1. Report current audio-disk usage for the dashboard via device_status.
  2. When usage crosses a hard cap, delete the oldest-expiring /
     already-synced files until we're back under the warning threshold.
     If there are no deletable files left (everything unsynced or
     human-verified), touch ``/control/halt_recordings`` so
     batdetect-service stops capturing, and log a capture_errors row.

**Deletion order** matches Stage D2 spec:

  a) tier 4 files past expires_at
  b) tier 2 files past expires_at
  c) tier 2 files not yet expired (oldest expires_at first)
  d) tier 1 files confirmed on OneDrive (oldest detection_time first)

**Protection rules** — a file is never touched if:

  * ``verified_class IS NOT NULL`` (a reviewer has flagged it).
  * For tier 1 only, ``synced_remote_at IS NULL`` (OneDrive copy
    doesn't exist yet, so the local WAV is the only copy).

Tier 2/4 are never uploaded to OneDrive, so the ``synced_remote_at``
rule doesn't apply to them — they're local-ephemeral by design.
"""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

# -----------------------------------------------------------------------------
# Tunable thresholds (env-overridable). Defaults sized for the current
# Pi SD card (229 GB total, per the dashboard telemetry as of 2026-04-17).
# -----------------------------------------------------------------------------

DISK_WARNING_GB = int(os.getenv("DISK_WARNING_GB", "170"))
DISK_HARD_CAP_GB = int(os.getenv("DISK_HARD_CAP_GB", "180"))
DISK_TARGET_FREE_GB = int(os.getenv("DISK_TARGET_FREE_GB", "50"))  # informational

HALT_FLAG = Path("/control/halt_recordings")

STAGES = ("tier4_expired", "tier2_expired", "tier2_active", "tier1_synced")

_STAGE_SQL = {
    "tier4_expired": """
        SELECT id, audio_path
        FROM bat_detections
        WHERE storage_tier = 4
          AND expires_at IS NOT NULL AND expires_at < NOW()
          AND audio_path IS NOT NULL
          AND verified_class IS NULL
        ORDER BY expires_at ASC
        LIMIT 500
    """,
    "tier2_expired": """
        SELECT id, audio_path
        FROM bat_detections
        WHERE storage_tier = 2
          AND expires_at IS NOT NULL AND expires_at < NOW()
          AND audio_path IS NOT NULL
          AND verified_class IS NULL
        ORDER BY expires_at ASC
        LIMIT 500
    """,
    "tier2_active": """
        SELECT id, audio_path
        FROM bat_detections
        WHERE storage_tier = 2
          AND (expires_at IS NULL OR expires_at >= NOW())
          AND audio_path IS NOT NULL
          AND verified_class IS NULL
        ORDER BY expires_at ASC NULLS FIRST
        LIMIT 500
    """,
    "tier1_synced": """
        SELECT id, audio_path
        FROM bat_detections
        WHERE storage_tier = 1
          AND audio_path IS NOT NULL
          AND verified_class IS NULL
          AND synced_remote_at IS NOT NULL
        ORDER BY detection_time ASC
        LIMIT 500
    """,
}


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------

def _select_files_to_delete(candidates: List[dict], bytes_to_free: int) -> List[dict]:
    """Shortest prefix of `candidates` whose cumulative size_bytes hits the goal.

    `candidates` must already be ordered by the deletion-priority stage
    above. Each dict needs ``size_bytes``.
    """
    selected = []
    freed = 0
    for cand in candidates:
        selected.append(cand)
        freed += cand["size_bytes"]
        if freed >= bytes_to_free:
            break
    return selected


def _bytes_to_gb(n: int) -> float:
    return n / (1024 ** 3)


def _gb_to_bytes(gb: float) -> int:
    return int(gb * (1024 ** 3))


# -----------------------------------------------------------------------------
# DB + filesystem effects
# -----------------------------------------------------------------------------

def _fetch_stage_candidates(conn, stage: str) -> List[dict]:
    with conn.cursor() as cur:
        cur.execute(_STAGE_SQL[stage])
        rows = cur.fetchall()
    out = []
    for row_id, audio_path in rows:
        if not audio_path:
            continue
        try:
            size = os.path.getsize(audio_path)
        except (FileNotFoundError, OSError):
            # File missing on disk — null the row so we stop seeing it.
            _null_audio_row(conn, row_id)
            continue
        out.append({"id": row_id, "audio_path": audio_path, "size_bytes": size, "stage": stage})
    return out


def _null_audio_row(conn, row_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bat_detections SET audio_path = NULL, expires_at = NULL WHERE id = %s",
            (row_id,),
        )
    conn.commit()


def _delete_candidate(conn, cand: dict) -> int:
    """Remove the file and null the row. Returns bytes freed."""
    path = cand["audio_path"]
    size = cand["size_bytes"]
    try:
        os.remove(path)
    except FileNotFoundError:
        size = 0
    _null_audio_row(conn, cand["id"])
    return size


def _count_unsynced_tier1(conn) -> int:
    """Files we would've deleted at stage d but couldn't because OneDrive is behind."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM bat_detections
            WHERE storage_tier = 1
              AND audio_path IS NOT NULL
              AND synced_remote_at IS NULL
              AND verified_class IS NULL
        """)
        return cur.fetchone()[0]


def _log_capture_error(conn, message: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO capture_errors (service, error_type, message) VALUES (%s, %s, %s)",
                ("sync-service.disk_watchdog", "DiskQuotaHalt", message[:500]),
            )
        conn.commit()
    except Exception as e:
        print(f"[WATCHDOG] failed to log capture_error: {e}")


def _set_halt(active: bool) -> None:
    if active:
        HALT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        HALT_FLAG.touch()
    else:
        try:
            HALT_FLAG.unlink()
        except FileNotFoundError:
            pass


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def get_audio_disk_stats(bat_audio_dir: str) -> Dict[str, Optional[float]]:
    """Snapshot suitable for the deviceStatus Firestore doc."""
    try:
        usage = shutil.disk_usage(bat_audio_dir)
    except (FileNotFoundError, OSError):
        return {
            "audioDiskTotalGb": None,
            "audioDiskUsedGb": None,
            "audioDiskFreeGb": None,
            "audioDiskWarningGb": DISK_WARNING_GB,
            "audioDiskHardCapGb": DISK_HARD_CAP_GB,
            "audioHaltActive": HALT_FLAG.exists(),
        }
    return {
        "audioDiskTotalGb": round(_bytes_to_gb(usage.total), 2),
        "audioDiskUsedGb": round(_bytes_to_gb(usage.used), 2),
        "audioDiskFreeGb": round(_bytes_to_gb(usage.free), 2),
        "audioDiskWarningGb": DISK_WARNING_GB,
        "audioDiskHardCapGb": DISK_HARD_CAP_GB,
        "audioHaltActive": HALT_FLAG.exists(),
    }


def enforce_disk_quota(
    conn,
    bat_audio_dir: str,
    disk_usage_fn: Callable = shutil.disk_usage,
) -> Dict:
    """Apply the deletion ladder. Returns a summary dict suitable for logging.

    ``disk_usage_fn`` exists as a seam for unit tests.
    """
    try:
        usage = disk_usage_fn(bat_audio_dir)
    except (FileNotFoundError, OSError) as e:
        return {
            "action": "error",
            "used_gb": None,
            "error": str(e),
            "files_deleted": 0,
            "gb_freed": 0.0,
            "halt_recordings": False,
        }

    used_gb = _bytes_to_gb(usage.used)

    if used_gb < DISK_WARNING_GB:
        _set_halt(False)
        return {
            "action": "none",
            "used_gb": round(used_gb, 2),
            "files_deleted": 0,
            "gb_freed": 0.0,
            "halt_recordings": False,
        }

    if used_gb < DISK_HARD_CAP_GB:
        # Over soft warning, not yet hard cap — log but don't delete.
        return {
            "action": "warning",
            "used_gb": round(used_gb, 2),
            "files_deleted": 0,
            "gb_freed": 0.0,
            "halt_recordings": HALT_FLAG.exists(),
        }

    # Over hard cap — delete until we're back under the warning threshold.
    target_used_bytes = _gb_to_bytes(DISK_WARNING_GB)
    bytes_to_free = max(0, usage.used - target_used_bytes)
    total_freed = 0
    files_deleted = 0

    for stage in STAGES:
        if total_freed >= bytes_to_free:
            break
        candidates = _fetch_stage_candidates(conn, stage)
        picks = _select_files_to_delete(candidates, bytes_to_free - total_freed)
        for cand in picks:
            try:
                freed = _delete_candidate(conn, cand)
                total_freed += freed
                files_deleted += 1
                if total_freed >= bytes_to_free:
                    break
            except Exception as e:
                print(f"[WATCHDOG] delete failed for {cand['audio_path']}: {e}")

    # Re-check actual disk usage (in case other processes freed / consumed).
    try:
        new_usage = disk_usage_fn(bat_audio_dir)
        new_used_gb = _bytes_to_gb(new_usage.used)
    except (FileNotFoundError, OSError):
        new_used_gb = used_gb - _bytes_to_gb(total_freed)

    if new_used_gb >= DISK_WARNING_GB:
        unsynced = _count_unsynced_tier1(conn)
        msg = (
            f"Disk at {new_used_gb:.1f} GB after deleting {files_deleted} files "
            f"({_bytes_to_gb(total_freed):.1f} GB freed); halting recordings. "
            f"{unsynced} tier-1 files blocked (awaiting OneDrive sync)."
        )
        print(f"[WATCHDOG] {msg}")
        _set_halt(True)
        _log_capture_error(conn, msg)
        return {
            "action": "halted_recordings",
            "used_gb": round(new_used_gb, 2),
            "files_deleted": files_deleted,
            "gb_freed": round(_bytes_to_gb(total_freed), 2),
            "halt_recordings": True,
            "unsynced_files_blocking": unsynced,
        }

    _set_halt(False)
    return {
        "action": "deleted_files",
        "used_gb": round(new_used_gb, 2),
        "files_deleted": files_deleted,
        "gb_freed": round(_bytes_to_gb(total_freed), 2),
        "halt_recordings": False,
    }
