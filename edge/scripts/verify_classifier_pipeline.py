#!/usr/bin/env python3
"""End-to-end sanity check for the bat classifier pipeline.

Run before flipping ENABLE_GROUPS_CLASSIFIER=true on the Pi. Catches:
  * BatDetect2 version drift (pinned to 1.3.1 at training time)
  * api.process_audio() signature / return-shape changes
  * 32-dim feature extractor mismatches with the saved classifier
  * Detection-dict key renames that would silently blank out columns
  * Classifier output outside the expected 5-class set

Usage (inside the batdetect-service container on the Pi):
    docker compose exec batdetect-service \\
        python edge/scripts/verify_classifier_pipeline.py \\
        --wav /bat_audio/some_capture.wav

Usage (locally, with batdetect2==1.3.1 installed in a venv):
    python edge/scripts/verify_classifier_pipeline.py \\
        --wav /path/to/sample.wav

Exits 0 with "OK" on success, non-zero on any failure.
"""

import argparse
import sys
from pathlib import Path

EXPECTED_BATDETECT2_VERSION = "1.3.1"
EXPECTED_FEATURE_DIM = 32
EXPECTED_DETECTION_KEYS = {"det_prob", "start_time", "end_time", "low_freq", "high_freq", "class"}
EXPECTED_CLASSES = {"EPFU_LANO", "LABO", "LACI", "MYSP", "PESU"}
CLASSIFIER_DET_THRESHOLD = 0.5


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def locate_classifier_module():
    """Put the real classifier.py on sys.path (container or Mac layout)."""
    script_dir = Path(__file__).resolve().parent
    candidates = [
        Path("/app/src"),                                      # inside batdetect-service container
        script_dir.parent / "batdetect-service" / "src",       # repo layout (edge/scripts/.. -> edge/)
        script_dir.parent / "analysis-api" / "src",            # fallback — same file
    ]
    for p in candidates:
        if (p / "classifier.py").exists():
            sys.path.insert(0, str(p))
            return p
    fail(f"classifier.py not found in any of: {[str(c) for c in candidates]}")


def locate_model():
    script_dir = Path(__file__).resolve().parent
    candidates = [
        Path("/app/models/groups_model.pt"),                   # container
        script_dir.parent.parent / "docker" / "models" / "groups_model.pt",  # repo
    ]
    for p in candidates:
        if p.exists():
            return p
    fail(f"groups_model.pt not found in any of: {[str(c) for c in candidates]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wav", required=True, help="Path to a .wav file with bat calls")
    parser.add_argument("--model", default=None, help="Override classifier model path")
    args = parser.parse_args()

    wav_path = Path(args.wav)
    if not wav_path.exists():
        fail(f"wav not found: {wav_path}")

    # 1. Version check — must match training to keep the feature extractor stable.
    try:
        import batdetect2
        from batdetect2 import api as bat_api
    except ImportError as e:
        fail(f"cannot import batdetect2: {e}")

    version = getattr(batdetect2, "__version__", None)
    if version != EXPECTED_BATDETECT2_VERSION:
        fail(
            f"batdetect2 version mismatch — expected {EXPECTED_BATDETECT2_VERSION}, "
            f"got {version}. Classifier was trained against {EXPECTED_BATDETECT2_VERSION}; "
            f"a different feature extractor will produce undefined predictions."
        )
    print(f"[1/6] batdetect2 version OK: {version}")

    # 2. Classifier module + model file discovery.
    locate_classifier_module()
    from classifier import classify, load_groups_classifier  # noqa: E402

    model_path = Path(args.model) if args.model else locate_model()
    print(f"[2/6] classifier module + model located: {model_path}")

    # 3. Model loads and exposes the expected scheme.
    model, ckpt = load_groups_classifier(str(model_path))
    class_names = set(ckpt.get("class_names", []))
    if class_names != EXPECTED_CLASSES:
        fail(f"checkpoint class_names mismatch — expected {EXPECTED_CLASSES}, got {class_names}")
    print(f"[3/6] model loaded — classes={sorted(class_names)}")

    # 4. Run BatDetect2's feature pipeline on the input WAV.
    audio = bat_api.load_audio(str(wav_path))
    detections, features, _ = bat_api.process_audio(audio)
    if features is None or len(features) == 0:
        fail(f"process_audio returned no features for {wav_path}")
    if features.shape[1] != EXPECTED_FEATURE_DIM:
        fail(
            f"feature dim mismatch — expected {EXPECTED_FEATURE_DIM}, got {features.shape[1]}. "
            f"The frozen extractor has changed; retrain or pick a different batdetect2 version."
        )
    print(f"[4/6] process_audio OK — {len(detections)} detections, features.shape={features.shape}")

    # 5. Detection dicts carry the keys main.py and analysis-api rely on.
    if detections:
        sample_keys = set(detections[0].keys())
        missing = EXPECTED_DETECTION_KEYS - sample_keys
        if missing:
            fail(
                f"detection dict is missing expected keys: {missing}. "
                f"Seen: {sorted(sample_keys)}. Update main.py or the verify script."
            )
    print(f"[5/6] detection dict keys OK: {sorted(EXPECTED_DETECTION_KEYS)}")

    # 6. Classifier runs end-to-end on high-confidence detections.
    high_conf_mask = [d.get("det_prob", 0.0) > CLASSIFIER_DET_THRESHOLD for d in detections]
    if not any(high_conf_mask):
        fail(
            f"no detections above det_prob > {CLASSIFIER_DET_THRESHOLD} in {wav_path}. "
            f"Use a sample known to contain bat calls."
        )
    high_conf_feats = features[high_conf_mask]
    preds = classify(high_conf_feats, model, ckpt)

    pred_classes = {p["predicted_class"] for p in preds}
    unexpected = pred_classes - EXPECTED_CLASSES
    if unexpected:
        fail(f"classifier produced labels outside the expected set: {unexpected}")
    print(
        f"[6/6] classifier OK — {len(preds)} predictions, "
        f"classes seen: {sorted(pred_classes)}"
    )

    print("\nOK")


if __name__ == "__main__":
    main()
