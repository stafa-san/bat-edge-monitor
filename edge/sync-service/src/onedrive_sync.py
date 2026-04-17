"""Tier 1 archival sync to UC OneDrive via rclone.

Runs on an interval (not every cycle — uploads are slow) from
sync-service/main.py. For each tier-1 row with ``audio_path IS NOT NULL``
and ``remote_audio_path IS NULL``, copies the WAV to OneDrive and
records the remote path + timestamp. **Never deletes the local file** —
that's the disk watchdog's job once pressure hits the hard cap.

Idempotency: if a previous run uploaded the file but the DB update
failed, we detect it via ``rclone lsjson`` and just backfill the row,
so network retries don't produce duplicate uploads.

Safety:
  * All rclone calls go through ``_run_rclone`` with a bounded timeout.
  * If the rclone binary is missing (e.g., sync-service was rebuilt
    without it) the orchestrator returns ``action="error"`` and keeps
    sync-service healthy — no crash, no partial state.
  * ``ENABLE_ONEDRIVE_SYNC=false`` short-circuits before any subprocess
    work, so branch-pullers don't get surprised by missing OAuth config.
"""

import json
import os
import subprocess
from typing import Callable, Dict, List, Optional

# -----------------------------------------------------------------------------
# Defaults — overridable via env. Config is passed into the public API so
# tests can construct a clean dict without touching the environment.
# -----------------------------------------------------------------------------

DEFAULT_RCLONE_BIN = "rclone"
DEFAULT_MAX_FILES_PER_BATCH = 50
DEFAULT_TIMEOUT_PER_FILE_SEC = 300
DEFAULT_RCLONE_VERSION_TIMEOUT_SEC = 10
DEFAULT_LSJSON_TIMEOUT_SEC = 30

LOCAL_AUDIO_PREFIX = "/bat_audio/"


def config_from_env() -> Dict:
    """Read the OneDrive sync config from environment variables."""
    return {
        "rclone_remote_name": os.getenv("ONEDRIVE_REMOTE_NAME", "onedrive"),
        "remote_base_path": os.getenv("ONEDRIVE_REMOTE_BASE_PATH", "Bat Recordings from pi01"),
        "rclone_bin": os.getenv("RCLONE_BIN", DEFAULT_RCLONE_BIN),
        "max_files_per_batch": int(os.getenv("ONEDRIVE_MAX_FILES_PER_BATCH", str(DEFAULT_MAX_FILES_PER_BATCH))),
        "timeout_per_file_sec": int(os.getenv("ONEDRIVE_TIMEOUT_PER_FILE_SEC", str(DEFAULT_TIMEOUT_PER_FILE_SEC))),
        "pi_site": os.getenv("PI_SITE", "pi01"),
        "dry_run": os.getenv("ONEDRIVE_DRY_RUN", "false").lower() == "true",
    }


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------

def _build_remote_path(local_path: str, pi_site: str, remote_base: str) -> str:
    """Translate an on-Pi tier-1 path to its OneDrive counterpart.

    >>> _build_remote_path(
    ...     "/bat_audio/tier1_permanent/PESU/pi01_20260417T143022Z.wav",
    ...     "pi01", "Bat Recordings from pi01",
    ... )
    'Bat Recordings from pi01/tier1_permanent/PESU/pi01_20260417T143022Z.wav'

    ``pi_site`` is accepted for forward-compat (future per-site routing
    logic) but isn't used in the path today — ``remote_base`` already
    bakes in the site identifier.
    """
    del pi_site  # reserved
    if not local_path.startswith(LOCAL_AUDIO_PREFIX):
        raise ValueError(
            f"refusing to build remote path — local path {local_path!r} "
            f"is outside {LOCAL_AUDIO_PREFIX}"
        )
    relative = local_path[len(LOCAL_AUDIO_PREFIX):]
    base = remote_base.rstrip("/")
    return f"{base}/{relative}"


# -----------------------------------------------------------------------------
# Subprocess seam — tests monkeypatch this function, not subprocess.run.
# -----------------------------------------------------------------------------

def _run_rclone(args: List[str], timeout: int) -> Dict:
    """Run rclone with args; return a dict of (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": b"", "stderr": b"timeout"}
    except FileNotFoundError:
        return {"returncode": -1, "stdout": b"", "stderr": b"rclone binary not found"}


# -----------------------------------------------------------------------------
# rclone-calling helpers
# -----------------------------------------------------------------------------

def _rclone_is_available(config: Dict) -> bool:
    res = _run_rclone(
        [config["rclone_bin"], "version"],
        timeout=DEFAULT_RCLONE_VERSION_TIMEOUT_SEC,
    )
    return res["returncode"] == 0


def _remote_exists(config: Dict, remote_path: str) -> Optional[bool]:
    """Return True/False for file presence, None if we couldn't tell.

    ``rclone lsjson`` on a specific file returns a one-element array if
    the file exists and ``[]`` if the parent dir exists but the file
    doesn't. It errors on missing parent dirs — in that case we don't
    know (parent may not exist yet), so we conservatively return False
    so the caller uploads.
    """
    target = f"{config['rclone_remote_name']}:{remote_path}"
    res = _run_rclone(
        [config["rclone_bin"], "lsjson", "--files-only", target],
        timeout=DEFAULT_LSJSON_TIMEOUT_SEC,
    )
    if res["returncode"] != 0:
        return False
    try:
        parsed = json.loads(res["stdout"].decode("utf-8") or "[]")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return len(parsed) > 0


def _upload_file(local_path: str, remote_path: str, config: Dict) -> Dict:
    """Copy (not move) one local WAV to OneDrive. Returns a result dict."""
    target = f"{config['rclone_remote_name']}:{remote_path}"
    bytes_on_disk = 0
    try:
        bytes_on_disk = os.path.getsize(local_path)
    except OSError:
        return {"success": False, "bytes": 0, "error": "local_file_missing"}

    if config.get("dry_run"):
        return {"success": True, "bytes": bytes_on_disk, "error": None, "dry_run": True}

    res = _run_rclone(
        [
            config["rclone_bin"], "copyto",
            local_path, target,
            "--timeout", "60s",
            "--contimeout", "30s",
            "--low-level-retries", "3",
            "--retries", "1",
        ],
        timeout=config.get("timeout_per_file_sec", DEFAULT_TIMEOUT_PER_FILE_SEC),
    )
    if res["returncode"] != 0:
        stderr_text = res["stderr"].decode("utf-8", errors="replace")[:500]
        return {"success": False, "bytes": 0, "error": stderr_text or "nonzero_exit"}
    return {"success": True, "bytes": bytes_on_disk, "error": None}


# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------

_CANDIDATE_SQL = """
    SELECT id, audio_path, detection_time
    FROM bat_detections
    WHERE storage_tier = 1
      AND audio_path IS NOT NULL
      AND remote_audio_path IS NULL
    ORDER BY detection_time ASC
    LIMIT %s
"""


def _find_upload_candidates(conn, limit: int) -> List[Dict]:
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, (limit,))
        rows = cur.fetchall()
    return [
        {"id": r[0], "audio_path": r[1], "detection_time": r[2]}
        for r in rows
    ]


def _mark_synced(conn, row_id: int, full_remote_path: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bat_detections
               SET remote_audio_path = %s,
                   synced_remote_at  = NOW()
             WHERE id = %s
            """,
            (full_remote_path, row_id),
        )
    conn.commit()


# -----------------------------------------------------------------------------
# Public orchestrator
# -----------------------------------------------------------------------------

def sync_tier1_to_onedrive(conn, config: Optional[Dict] = None) -> Dict:
    """Find tier-1 files that need uploading and push them to OneDrive.

    Safe to call when the feature flag is off — the caller just won't
    pass a config, or sets config["_enabled"] = False.
    """
    if config is None:
        config = config_from_env()

    if not config.get("_enabled", True):
        return {
            "action": "disabled",
            "candidates_found": 0,
            "uploads_attempted": 0,
            "uploads_succeeded": 0,
            "uploads_failed": 0,
            "bytes_uploaded": 0,
            "errors": [],
        }

    if not _rclone_is_available(config):
        return {
            "action": "error",
            "candidates_found": 0,
            "uploads_attempted": 0,
            "uploads_succeeded": 0,
            "uploads_failed": 0,
            "bytes_uploaded": 0,
            "errors": [{"file": None, "error": "rclone not available"}],
        }

    limit = config.get("max_files_per_batch", DEFAULT_MAX_FILES_PER_BATCH)
    candidates = _find_upload_candidates(conn, limit)
    if not candidates:
        return {
            "action": "no_candidates",
            "candidates_found": 0,
            "uploads_attempted": 0,
            "uploads_succeeded": 0,
            "uploads_failed": 0,
            "bytes_uploaded": 0,
            "errors": [],
        }

    pi_site = config.get("pi_site", "pi01")
    remote_base = config["remote_base_path"]

    uploads_attempted = 0
    uploads_succeeded = 0
    uploads_failed = 0
    bytes_uploaded = 0
    errors: List[Dict] = []

    for cand in candidates:
        local_path = cand["audio_path"]

        if not os.path.exists(local_path):
            # File vanished between tier-1 insert and sync — skip without
            # marking synced. Leave the row for the watchdog / operator.
            errors.append({"file": local_path, "error": "local_file_missing"})
            uploads_failed += 1
            continue

        try:
            remote_path = _build_remote_path(local_path, pi_site, remote_base)
        except ValueError as e:
            errors.append({"file": local_path, "error": str(e)})
            uploads_failed += 1
            continue

        full_remote = f"{config['rclone_remote_name']}:{remote_path}"

        # Idempotency: if the file is already on OneDrive from a prior run
        # where the DB update failed, just backfill the row — no re-upload.
        already_there = _remote_exists(config, remote_path)
        if already_there is True:
            _mark_synced(conn, cand["id"], full_remote)
            continue

        uploads_attempted += 1
        result = _upload_file(local_path, remote_path, config)
        if result["success"]:
            _mark_synced(conn, cand["id"], full_remote)
            uploads_succeeded += 1
            bytes_uploaded += result["bytes"]
        else:
            uploads_failed += 1
            errors.append({"file": local_path, "error": result["error"]})

    return {
        "action": "synced",
        "candidates_found": len(candidates),
        "uploads_attempted": uploads_attempted,
        "uploads_succeeded": uploads_succeeded,
        "uploads_failed": uploads_failed,
        "bytes_uploaded": bytes_uploaded,
        "errors": errors,
    }
