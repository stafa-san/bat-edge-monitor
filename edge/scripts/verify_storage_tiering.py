#!/usr/bin/env python3
"""Unit tests for storage tiering + disk watchdog logic.

Run from repo root:

    python edge/scripts/verify_storage_tiering.py

Exits 0 when all tests pass, 1 otherwise.

Stdlib only — no pytest dependency — so this runs on the Pi, in CI, or
on a grad-student's laptop without any setup.
"""

import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

# Make `storage` (batdetect-service) and `disk_watchdog` (sync-service) importable.
STORAGE_SRC = REPO_ROOT / "edge" / "batdetect-service" / "src"
WATCHDOG_SRC = REPO_ROOT / "edge" / "sync-service" / "src"
for required in [STORAGE_SRC / "storage.py", WATCHDOG_SRC / "disk_watchdog.py"]:
    if not required.exists():
        print(f"FAIL: cannot find {required}", file=sys.stderr)
        sys.exit(1)
sys.path.insert(0, str(STORAGE_SRC))
sys.path.insert(0, str(WATCHDOG_SRC))

import storage  # noqa: E402
import disk_watchdog  # noqa: E402


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def pred(cls, conf):
    return {"predicted_class": cls, "prediction_confidence": conf}


def make_wav(path: Path, size_bytes: int = 1024) -> Path:
    """Create a fake WAV file of the requested size; content doesn't matter.

    Uses ``truncate`` to produce a sparse file so tests requesting multi-GB
    "files" don't actually consume the host's disk. ``os.path.getsize``
    still reports the logical size, which is all the watchdog reads.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    return path


# -----------------------------------------------------------------------------
# determine_tier
# -----------------------------------------------------------------------------

def test_tier_4_empty():
    assert storage.determine_tier([]) == 4


def test_tier_4_all_below_max():
    preds = [pred("MYSP", 0.2), pred("LABO", 0.25)]
    assert storage.determine_tier(preds) == 4


def test_tier_4_single_very_low():
    assert storage.determine_tier([pred("PESU", 0.29)]) == 4


def test_tier_1_rare_high_conf():
    assert storage.determine_tier([pred("PESU", 0.95)]) == 1


def test_tier_1_triggered_at_boundary():
    # Exactly TIER1_CONFIDENCE_MIN should qualify (>=, not >).
    assert storage.determine_tier([pred("LACI", 0.9)]) == 1


def test_tier_1_not_triggered_for_common_class():
    # EPFU_LANO is not rare — high conf drops to tier 2.
    assert storage.determine_tier([pred("EPFU_LANO", 0.95)]) == 2


def test_tier_1_not_triggered_by_rare_medium_conf():
    # Rare class but confidence below tier-1 threshold.
    assert storage.determine_tier([pred("PESU", 0.7)]) == 2


def test_tier_2_any_above_min():
    assert storage.determine_tier([pred("MYSP", 0.7)]) == 2


def test_tier_2_boundary():
    assert storage.determine_tier([pred("LABO", 0.5)]) == 2


def test_tier_3_between_thresholds():
    # Above TIER4_CONFIDENCE_MAX (0.3) but below TIER2_CONFIDENCE_MIN (0.5).
    assert storage.determine_tier([pred("PESU", 0.4)]) == 3


def test_tier_3_mixed_below_min():
    preds = [pred("PESU", 0.45), pred("EPFU_LANO", 0.35)]
    assert storage.determine_tier(preds) == 3


def test_tier_1_wins_over_tier_2_mixed():
    # File with PESU 0.95 and EPFU_LANO 0.6 — rare-high-conf promotes to tier 1.
    preds = [pred("PESU", 0.95), pred("EPFU_LANO", 0.6)]
    assert storage.determine_tier(preds) == 1


# -----------------------------------------------------------------------------
# pick_class_folder
# -----------------------------------------------------------------------------

def test_pick_folder_single_class():
    assert storage.pick_class_folder(1, [pred("PESU", 0.95)]) == "PESU"


def test_pick_folder_multi_class_routes_to_rarest():
    # Both rare, but PESU outranks LABO in CLASS_PRIORITY_ORDER.
    preds = [pred("LABO", 0.95), pred("PESU", 0.91)]
    assert storage.pick_class_folder(1, preds) == "PESU"


def test_pick_folder_rare_over_common():
    preds = [pred("EPFU_LANO", 0.95), pred("LACI", 0.92)]
    assert storage.pick_class_folder(1, preds) == "LACI"


def test_pick_folder_none_for_tier_2():
    assert storage.pick_class_folder(2, [pred("MYSP", 0.7)]) is None


def test_pick_folder_none_for_tier_3():
    assert storage.pick_class_folder(3, [pred("PESU", 0.4)]) is None


def test_pick_folder_none_for_tier_4():
    assert storage.pick_class_folder(4, []) is None


# -----------------------------------------------------------------------------
# build_filename
# -----------------------------------------------------------------------------

def test_build_filename_utc():
    ts = datetime(2026, 4, 17, 14, 30, 22, tzinfo=timezone.utc)
    assert storage.build_filename("pi01", ts) == "pi01_20260417T143022Z.wav"


def test_build_filename_treats_naive_as_utc():
    # main.py uses datetime.utcnow() which is naive; must land on UTC.
    ts = datetime(2026, 4, 17, 14, 30, 22)
    assert storage.build_filename("pi01", ts) == "pi01_20260417T143022Z.wav"


def test_build_filename_converts_non_utc():
    # A PDT timezone (-7) at local 07:30:22 == UTC 14:30:22.
    from datetime import timedelta
    pdt = timezone(timedelta(hours=-7))
    ts = datetime(2026, 4, 17, 7, 30, 22, tzinfo=pdt)
    assert storage.build_filename("pi02", ts) == "pi02_20260417T143022Z.wav"


# -----------------------------------------------------------------------------
# compute_expires_at
# -----------------------------------------------------------------------------

def test_expires_tier_1_is_none():
    assert storage.compute_expires_at(1) is None


def test_expires_tier_3_is_none():
    assert storage.compute_expires_at(3) is None


def test_expires_tier_2_is_30_days():
    now = datetime(2026, 4, 17, tzinfo=timezone.utc)
    assert storage.compute_expires_at(2, now=now) == datetime(2026, 5, 17, tzinfo=timezone.utc)


def test_expires_tier_4_is_7_days():
    now = datetime(2026, 4, 17, tzinfo=timezone.utc)
    assert storage.compute_expires_at(4, now=now) == datetime(2026, 4, 24, tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# archive_wav
# -----------------------------------------------------------------------------

def test_archive_tier_1_goes_to_class_subfolder():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = make_wav(tmp / "capture.wav")
        bat_audio = tmp / "bat_audio"
        detection_time = datetime(2026, 4, 17, 14, 30, 22, tzinfo=timezone.utc)

        dest, expires = storage.archive_wav(
            str(src), tier=1, class_folder="PESU",
            site_id="pi01", detection_time=detection_time,
            bat_audio_dir=str(bat_audio),
        )
        assert dest == bat_audio / "tier1_permanent" / "PESU" / "pi01_20260417T143022Z.wav"
        assert dest.exists()
        assert not src.exists(), "source wav should be moved, not copied"
        assert expires is None


def test_archive_tier_2_is_flat():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = make_wav(tmp / "capture.wav")
        bat_audio = tmp / "bat_audio"
        detection_time = datetime(2026, 4, 17, 14, 30, 22, tzinfo=timezone.utc)

        dest, expires = storage.archive_wav(
            str(src), tier=2, class_folder=None,
            site_id="pi01", detection_time=detection_time,
            bat_audio_dir=str(bat_audio),
        )
        assert dest == bat_audio / "tier2_30day" / "pi01_20260417T143022Z.wav"
        assert dest.exists()
        assert expires is not None


def test_archive_tier_3_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = make_wav(tmp / "capture.wav")
        bat_audio = tmp / "bat_audio"
        detection_time = datetime(2026, 4, 17, 14, 30, 22, tzinfo=timezone.utc)

        dest, expires = storage.archive_wav(
            str(src), tier=3, class_folder=None,
            site_id="pi01", detection_time=detection_time,
            bat_audio_dir=str(bat_audio),
        )
        assert dest is None
        assert expires is None
        assert src.exists(), "source should be untouched for tier 3"
        assert not bat_audio.exists(), "no tier dir created"


def test_archive_tier_4_is_flat():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = make_wav(tmp / "capture.wav")
        bat_audio = tmp / "bat_audio"
        detection_time = datetime(2026, 4, 17, 14, 30, 22, tzinfo=timezone.utc)

        dest, expires = storage.archive_wav(
            str(src), tier=4, class_folder=None,
            site_id="pi01", detection_time=detection_time,
            bat_audio_dir=str(bat_audio),
        )
        assert dest == bat_audio / "tier4_anomaly" / "pi01_20260417T143022Z.wav"
        assert dest.exists()
        assert expires is not None


def test_archive_unknown_tier_raises():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = make_wav(tmp / "capture.wav")
        try:
            storage.archive_wav(
                str(src), tier=99, class_folder=None,
                site_id="pi01", detection_time=datetime.now(timezone.utc),
                bat_audio_dir=str(tmp / "bat_audio"),
            )
        except ValueError:
            return
        raise AssertionError("archive_wav should raise on unknown tier")


# -----------------------------------------------------------------------------
# Disk watchdog — pure _select_files_to_delete
# -----------------------------------------------------------------------------

def _cand(row_id, size_bytes, stage="tier2_active"):
    return {
        "id": row_id,
        "audio_path": f"/bat_audio/fake/{row_id}.wav",
        "size_bytes": size_bytes,
        "stage": stage,
    }


def test_select_files_empty():
    assert disk_watchdog._select_files_to_delete([], 100) == []


def test_select_files_shortest_prefix():
    cands = [_cand(1, 50), _cand(2, 50), _cand(3, 50)]
    picked = disk_watchdog._select_files_to_delete(cands, 80)
    # 50 + 50 = 100 >= 80; should include 2 candidates.
    assert [c["id"] for c in picked] == [1, 2]


def test_select_files_exact_match_stops_immediately():
    cands = [_cand(1, 100), _cand(2, 100)]
    picked = disk_watchdog._select_files_to_delete(cands, 100)
    assert [c["id"] for c in picked] == [1]


def test_select_files_insufficient_returns_all():
    cands = [_cand(1, 50), _cand(2, 50)]
    picked = disk_watchdog._select_files_to_delete(cands, 9999)
    assert [c["id"] for c in picked] == [1, 2]


# -----------------------------------------------------------------------------
# Disk watchdog — get_audio_disk_stats
# -----------------------------------------------------------------------------

def test_get_audio_disk_stats_returns_fields_for_real_dir():
    with tempfile.TemporaryDirectory() as tmp:
        stats = disk_watchdog.get_audio_disk_stats(tmp)
    assert "audioDiskTotalGb" in stats and stats["audioDiskTotalGb"] is not None
    assert "audioDiskUsedGb" in stats
    assert "audioDiskFreeGb" in stats
    assert stats["audioDiskWarningGb"] == disk_watchdog.DISK_WARNING_GB
    assert stats["audioDiskHardCapGb"] == disk_watchdog.DISK_HARD_CAP_GB
    assert isinstance(stats["audioHaltActive"], bool)


def test_get_audio_disk_stats_missing_dir():
    stats = disk_watchdog.get_audio_disk_stats("/this/does/not/exist/ever")
    assert stats["audioDiskTotalGb"] is None


# -----------------------------------------------------------------------------
# Disk watchdog — enforce_disk_quota with mocked DB + disk usage
# -----------------------------------------------------------------------------

class _FakeUsage:
    def __init__(self, used_gb, total_gb=229):
        self.used = int(used_gb * (1024 ** 3))
        self.total = int(total_gb * (1024 ** 3))
        self.free = self.total - self.used


class _RecordingFakeConn:
    """Minimal conn stand-in that records every SQL execute + returns curated rows."""

    def __init__(self):
        self.executed = []
        self.stage_rows = {s: [] for s in disk_watchdog.STAGES}
        self.unsynced_tier1_count = 0

    def set_stage_rows(self, stage, rows):
        self.stage_rows[stage] = rows

    def cursor(self):
        return _RecordingFakeCursor(self)

    def commit(self):
        pass


class _RecordingFakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        # Dispatch SELECT queries to the stage rows map.
        for stage, stage_sql in disk_watchdog._STAGE_SQL.items():
            if sql.strip() == stage_sql.strip():
                self._rows = self.conn.stage_rows[stage]
                return
        if "COUNT(*)" in sql and "synced_remote_at IS NULL" in sql:
            self._rows = [(self.conn.unsynced_tier1_count,)]
            return
        # UPDATEs / INSERTs — nothing to fetch.
        self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _reset_halt_flag_to(tmp_path):
    """Redirect the module-level HALT_FLAG at a temp location for the test."""
    disk_watchdog.HALT_FLAG = Path(tmp_path) / "halt_recordings"


def test_enforce_below_warning_does_nothing_and_clears_halt():
    with tempfile.TemporaryDirectory() as tmp:
        _reset_halt_flag_to(tmp)
        disk_watchdog.HALT_FLAG.touch()  # pretend it was set earlier
        conn = _RecordingFakeConn()
        result = disk_watchdog.enforce_disk_quota(
            conn, tmp, disk_usage_fn=lambda _: _FakeUsage(used_gb=100),
        )
        assert result["action"] == "none"
        assert result["halt_recordings"] is False
        assert not disk_watchdog.HALT_FLAG.exists()


def test_enforce_above_warning_below_hardcap_does_not_delete():
    with tempfile.TemporaryDirectory() as tmp:
        _reset_halt_flag_to(tmp)
        conn = _RecordingFakeConn()
        conn.set_stage_rows("tier2_active", [(1, "/bat_audio/a.wav")])
        result = disk_watchdog.enforce_disk_quota(
            conn, tmp, disk_usage_fn=lambda _: _FakeUsage(used_gb=175),
        )
        assert result["action"] == "warning"
        assert result["files_deleted"] == 0


def test_enforce_over_hardcap_deletes_in_tier_order():
    """First-priority stage drains fully before later stages are touched."""
    with tempfile.TemporaryDirectory() as tmp:
        _reset_halt_flag_to(tmp)
        bat_audio = Path(tmp) / "bat_audio"

        # Sparse files — size reported by os.path.getsize but no blocks allocated.
        def make(path, size_gb):
            return make_wav(bat_audio / path, size_bytes=int(size_gb * (1024 ** 3)))

        # One tier-4-expired file of 6 GB; one tier-2-active of 6 GB.
        t4_path = make("tier4_anomaly/old.wav", 6)
        t2_path = make("tier2_30day/fresh.wav", 6)

        conn = _RecordingFakeConn()
        conn.set_stage_rows("tier4_expired", [(1, str(t4_path))])
        conn.set_stage_rows("tier2_active", [(2, str(t2_path))])

        # Sim: 185 GB used → over 180 hard cap; need to free 15 GB to land
        # at 170 warning. Our two files = 12 GB total, still leaves 173 GB
        # after delete → halt required.
        call_count = {"n": 0}
        def disk_usage_fn(_):
            call_count["n"] += 1
            # First read reports the over-cap state; second (re-check)
            # reflects what remains after deletes.
            if call_count["n"] == 1:
                return _FakeUsage(used_gb=185)
            freed_gb = sum(
                c.get("size_bytes", 0) for c in []  # placeholder; updated below
            )
            # Use attribute on conn to track actual deletes.
            return _FakeUsage(used_gb=185 - conn._deleted_gb)

        conn._deleted_gb = 0
        _orig_delete = disk_watchdog._delete_candidate
        def tracking_delete(c, cand):
            bytes_freed = _orig_delete(c, cand)
            conn._deleted_gb += bytes_freed / (1024 ** 3)
            return bytes_freed
        disk_watchdog._delete_candidate = tracking_delete

        try:
            result = disk_watchdog.enforce_disk_quota(
                conn, str(bat_audio), disk_usage_fn=disk_usage_fn,
            )
        finally:
            disk_watchdog._delete_candidate = _orig_delete

        assert result["files_deleted"] == 2
        assert not t4_path.exists()
        assert not t2_path.exists()
        # tier-4-expired was stage 1, tier-2-active was stage 3 — both drained.
        # With both freed, we still project 173 GB > 170, so halt should trigger.
        assert result["action"] == "halted_recordings"
        assert result["halt_recordings"] is True
        assert disk_watchdog.HALT_FLAG.exists()


def test_enforce_over_hardcap_success_clears_halt():
    """Given enough to delete, we recover under warning and halt stays off."""
    with tempfile.TemporaryDirectory() as tmp:
        _reset_halt_flag_to(tmp)
        disk_watchdog.HALT_FLAG.touch()  # stale from a previous incident
        bat_audio = Path(tmp) / "bat_audio"

        paths = []
        for i in range(3):
            p = make_wav(bat_audio / "tier2_30day" / f"f{i}.wav", size_bytes=6 * 1024 ** 3)
            paths.append(p)

        conn = _RecordingFakeConn()
        conn.set_stage_rows("tier2_active", [(i + 1, str(p)) for i, p in enumerate(paths)])

        conn._deleted_gb = 0
        _orig = disk_watchdog._delete_candidate
        def tracking_delete(c, cand):
            b = _orig(c, cand)
            conn._deleted_gb += b / (1024 ** 3)
            return b
        disk_watchdog._delete_candidate = tracking_delete

        def disk_usage_fn(_):
            return _FakeUsage(used_gb=185 - conn._deleted_gb)

        try:
            result = disk_watchdog.enforce_disk_quota(
                conn, str(bat_audio), disk_usage_fn=disk_usage_fn,
            )
        finally:
            disk_watchdog._delete_candidate = _orig

        # 3 × 6 GB = 18 GB freed → below 170 GB warning. No halt.
        assert result["action"] == "deleted_files"
        assert result["halt_recordings"] is False
        assert not disk_watchdog.HALT_FLAG.exists()


def test_enforce_halts_when_all_candidates_protected():
    """No deletable rows at all — watchdog must halt, not silently pass."""
    with tempfile.TemporaryDirectory() as tmp:
        _reset_halt_flag_to(tmp)
        conn = _RecordingFakeConn()
        conn.unsynced_tier1_count = 42  # Pretend 42 tier-1 files are awaiting OneDrive.

        result = disk_watchdog.enforce_disk_quota(
            conn, tmp, disk_usage_fn=lambda _: _FakeUsage(used_gb=190),
        )
        assert result["action"] == "halted_recordings"
        assert result["unsynced_files_blocking"] == 42
        assert disk_watchdog.HALT_FLAG.exists()


def test_stage_sql_excludes_unsynced_tier1():
    """SQL text check: tier1_synced query requires synced_remote_at IS NOT NULL."""
    sql = disk_watchdog._STAGE_SQL["tier1_synced"]
    assert "storage_tier = 1" in sql
    assert "synced_remote_at IS NOT NULL" in sql
    assert "verified_class IS NULL" in sql


def test_stage_sql_excludes_verified():
    for stage_name, sql in disk_watchdog._STAGE_SQL.items():
        assert "verified_class IS NULL" in sql, f"{stage_name} missing verified protection"


# -----------------------------------------------------------------------------
# Runner
# -----------------------------------------------------------------------------

TESTS = [
    # determine_tier
    test_tier_4_empty,
    test_tier_4_all_below_max,
    test_tier_4_single_very_low,
    test_tier_1_rare_high_conf,
    test_tier_1_triggered_at_boundary,
    test_tier_1_not_triggered_for_common_class,
    test_tier_1_not_triggered_by_rare_medium_conf,
    test_tier_2_any_above_min,
    test_tier_2_boundary,
    test_tier_3_between_thresholds,
    test_tier_3_mixed_below_min,
    test_tier_1_wins_over_tier_2_mixed,
    # pick_class_folder
    test_pick_folder_single_class,
    test_pick_folder_multi_class_routes_to_rarest,
    test_pick_folder_rare_over_common,
    test_pick_folder_none_for_tier_2,
    test_pick_folder_none_for_tier_3,
    test_pick_folder_none_for_tier_4,
    # build_filename
    test_build_filename_utc,
    test_build_filename_treats_naive_as_utc,
    test_build_filename_converts_non_utc,
    # compute_expires_at
    test_expires_tier_1_is_none,
    test_expires_tier_3_is_none,
    test_expires_tier_2_is_30_days,
    test_expires_tier_4_is_7_days,
    # archive_wav
    test_archive_tier_1_goes_to_class_subfolder,
    test_archive_tier_2_is_flat,
    test_archive_tier_3_writes_nothing,
    test_archive_tier_4_is_flat,
    test_archive_unknown_tier_raises,
    # disk watchdog — pure
    test_select_files_empty,
    test_select_files_shortest_prefix,
    test_select_files_exact_match_stops_immediately,
    test_select_files_insufficient_returns_all,
    # disk watchdog — stats
    test_get_audio_disk_stats_returns_fields_for_real_dir,
    test_get_audio_disk_stats_missing_dir,
    # disk watchdog — enforce
    test_enforce_below_warning_does_nothing_and_clears_halt,
    test_enforce_above_warning_below_hardcap_does_not_delete,
    test_enforce_over_hardcap_deletes_in_tier_order,
    test_enforce_over_hardcap_success_clears_halt,
    test_enforce_halts_when_all_candidates_protected,
    # disk watchdog — SQL protection assertions
    test_stage_sql_excludes_unsynced_tier1,
    test_stage_sql_excludes_verified,
]


def main():
    failed = []
    for t in TESTS:
        try:
            t()
            print(f"  [OK]   {t.__name__}")
        except Exception as e:
            print(f"  [FAIL] {t.__name__}: {e.__class__.__name__}: {e}")
            traceback.print_exc()
            failed.append(t.__name__)
    print()
    if failed:
        print(f"{len(failed)}/{len(TESTS)} tests failed: {failed}")
        sys.exit(1)
    print(f"All {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
