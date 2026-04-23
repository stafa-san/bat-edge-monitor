#!/usr/bin/env python3
"""Golden-file regression test for the bat detection pipeline.

Run this against a known-good bat WAV after:
  * every `docker compose build batdetect-service` rebuild
  * every model update
  * every BatDetect2 / torch / numpy version bump
  * before sending the Pi out to a new field site

Unlike ``verify_classifier_pipeline.py`` (which validates pipeline
*structure* — feature dims, class names, key shapes), this script
validates pipeline *output* — it asserts the detector actually sees
the bats in a WAV you know contains bats.

This catches the BATDETECT2_STABILITY_FIX.md failure mode: BatDetect2
silently returns 0 raw detections on audio that should obviously
detect, with no exception and no warning. If that happens on the Pi
in production, it eats a night's worth of bat data without any
observable signal until the morning summary. Running this script
after every rebuild catches it at the bench instead.

Usage (inside the batdetect-service container):
    docker compose exec batdetect-service \\
        python /app/edge/scripts/test_pipeline_golden.py \\
        --wav /app/test_wavs/known_bat_activity.wav \\
        --min-raw-detections 20

Usage (locally, with batdetect2==1.3.1 installed):
    python edge/scripts/test_pipeline_golden.py \\
        --wav /path/to/known_bat.wav \\
        --min-raw-detections 20

Exits 0 on pass, non-zero with a loud message on fail.
"""

import argparse
import sys
from pathlib import Path


def fail(msg: str, exit_code: int = 1):
    print(f"\nFAIL: {msg}\n", file=sys.stderr)
    sys.exit(exit_code)


def main():
    ap = argparse.ArgumentParser(
        description="Golden-file regression test for the bat detection pipeline.",
    )
    ap.add_argument(
        "--wav", required=True, type=Path,
        help="Path to a WAV known to contain bat calls.",
    )
    ap.add_argument(
        "--min-raw-detections", type=int, default=10,
        help=(
            "Fail if BatDetect2 returns fewer than this many raw detections "
            "at the diagnostic threshold (default 10). Set per-file to what "
            "you've historically observed — doesn't need to be exact, just "
            "enough that a regression to zero will be caught."
        ),
    )
    ap.add_argument(
        "--min-max-det-prob", type=float, default=0.3,
        help=(
            "Fail if the highest det_prob in the file is below this. A "
            "healthy model on real bat audio should produce at least one "
            "detection above 0.3 (default)."
        ),
    )
    args = ap.parse_args()

    if not args.wav.exists():
        fail(f"WAV not found: {args.wav}")

    # Import inside main so --help works on machines without torch/batdetect2.
    try:
        import numpy as np  # noqa: F401
        import torch
        from batdetect2 import api as bat_api
    except ImportError as e:
        fail(f"Missing dependency: {e}. Run inside the batdetect-service container.")

    # Match production behaviour: pin RNG before the forward pass so the
    # result is reproducible run-to-run, and so we test the same code
    # path that ``bat_pipeline.py`` takes.
    torch.manual_seed(0)

    print(f"[GOLDEN] Loading {args.wav}...")
    config = bat_api.get_config()
    # Diagnostic threshold so we see sub-user-threshold emissions too.
    config["detection_threshold"] = 0.1

    audio = bat_api.load_audio(str(args.wav))
    duration_s = len(audio) / int(config.get("target_samp_rate", 256000))
    print(f"[GOLDEN] Audio: {len(audio)} samples ≈ {duration_s:.1f} s "
          f"@ {config.get('target_samp_rate')} Hz")

    print(f"[GOLDEN] Running BatDetect2...")
    detections, features, _ = bat_api.process_audio(audio, config=config)
    raw_count = len(detections)
    max_prob = max((d.get("det_prob", 0.0) for d in detections), default=0.0)

    print(f"[GOLDEN] raw_count    = {raw_count}")
    print(f"[GOLDEN] max_det_prob = {max_prob:.3f}")
    if detections:
        by_class = {}
        for d in detections:
            c = d.get("class", "?")
            by_class[c] = by_class.get(c, 0) + 1
        top = sorted(by_class.items(), key=lambda kv: -kv[1])[:5]
        print(f"[GOLDEN] top classes  = {top}")

    # Assertions.
    if raw_count < args.min_raw_detections:
        fail(
            f"raw_count={raw_count} below minimum {args.min_raw_detections}. "
            f"This is the degenerate-state signature from BATDETECT2_STABILITY_FIX.md. "
            f"Do NOT deploy this container to the field — the detector is broken. "
            f"Rebuild with `docker compose build --no-cache batdetect-service` "
            f"and re-run this test."
        )
    if max_prob < args.min_max_det_prob:
        fail(
            f"max_det_prob={max_prob:.3f} below minimum {args.min_max_det_prob}. "
            f"Detector is seeing activity but every emission is low-confidence — "
            f"either the WAV doesn't actually contain bats (wrong golden file), "
            f"or the detector has drifted far from training distribution."
        )

    print(f"\nPASS — {raw_count} raw detections, max_det_prob={max_prob:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
