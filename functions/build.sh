#!/usr/bin/env bash
# Sync canonical pipeline modules + model file into this functions/ dir
# right before `firebase deploy --only functions`. Wired as a predeploy
# hook in firebase.json so users never have to run this manually.
#
# Why: Cloud Functions packages only the functions/ dir. We want a
# single source of truth for the bat-analysis pipeline (it lives in
# edge/batdetect-service/src/), not drift-prone duplicates. This
# script enforces that.

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"

src_dir="$repo/edge/batdetect-service/src"
model_src="$repo/docker/models/groups_model.pt"

for f in bat_pipeline.py audio_validator.py classifier.py; do
    src="$src_dir/$f"
    if [[ ! -f "$src" ]]; then
        echo "FAIL: $src not found — cannot sync shared pipeline" >&2
        exit 1
    fi
    cp "$src" "$here/src/$f"
done

if [[ ! -f "$model_src" ]]; then
    echo "FAIL: $model_src not found — cannot sync classifier checkpoint" >&2
    exit 1
fi
mkdir -p "$here/models"
cp "$model_src" "$here/models/groups_model.pt"

echo "functions/ synced:"
echo "  src/bat_pipeline.py        <- edge/batdetect-service/src/"
echo "  src/audio_validator.py     <- edge/batdetect-service/src/"
echo "  src/classifier.py          <- edge/batdetect-service/src/"
echo "  models/groups_model.pt     <- docker/models/"
