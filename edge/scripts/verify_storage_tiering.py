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

# Make `storage` (batdetect-service) importable.
STORAGE_SRC = REPO_ROOT / "edge" / "batdetect-service" / "src"
if not (STORAGE_SRC / "storage.py").exists():
    print(f"FAIL: cannot find storage.py at {STORAGE_SRC}", file=sys.stderr)
    sys.exit(1)
sys.path.insert(0, str(STORAGE_SRC))

import storage  # noqa: E402


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def pred(cls, conf):
    return {"predicted_class": cls, "prediction_confidence": conf}


def make_wav(path: Path, size_bytes: int = 1024) -> Path:
    """Create a fake WAV file of approximate size; content doesn't matter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + b"\x00" * (size_bytes - 4))
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
