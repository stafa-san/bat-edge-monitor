# Reproducible Training Environment

This folder bundles everything needed to retrain the bat classifier from scratch
on any NVIDIA GPU machine (Vast.ai, RunPod, Lambda Labs, or your own hardware).

## What's in here

```
docker/
├── Dockerfile                # Builds the training environment
├── README.md                 # This file
├── scripts/
│   ├── extract_features.py   # Stage 1: BatDetect2 feature extraction
│   ├── train_classifier.py   # Stage 2: Train the classifier head
│   └── evaluate.py           # Stage 3: Validate on held-out audio
└── models/                   # Trained checkpoints (committed to repo for quick deploy)
    ├── frequency_model.pt    # 2-class sanity model
    ├── groups_model.pt       # 5-class production model ← USE THIS ON PI
    └── species_model.pt      # 9-class thesis comparison
```

## Prerequisites

- NVIDIA GPU with ≥12GB VRAM (RTX 3080+, A10, A100, or RTX 5090)
- NVIDIA Container Toolkit (`nvidia-docker`) installed
- Docker 20+
- Training data organized as one folder per species (see below)

## Data layout

Before running, arrange your audio into:

```
bat_data/
├── Epfu/           # one wav per recording, 192 kHz preferred
├── Labo/
├── Laci/
├── Lano/
├── Myle/
├── Mylu/
├── Myse/
├── Myso/
└── Pesu/
```

Folder names MUST be exactly these (case-sensitive). The classifier expects these
labels.

## Full retraining workflow

### 1. Build the image (one-time, ~5 minutes)

```bash
cd docker
docker build -t bat-trainer .
```

### 2. Start the container with data mounted

```bash
docker run --gpus all \
    -v $(pwd)/../bat_data:/workspace/bat_data \
    -v $(pwd)/output:/workspace/output \
    -it bat-trainer
```

You're now inside the container at `/workspace`.

### 3. Extract features for each species

```bash
for species in Epfu Labo Laci Lano Myle Mylu Myse Myso Pesu; do
    python extract_features.py --species $species
done
```

Takes ~15-30 min depending on data volume. Features saved to `/workspace/features/`.

### 4. Train all three models

```bash
python train_classifier.py --all
```

Takes ~10 min. Outputs to `/workspace/models/` and `/workspace/logs/`.

### 5. Validate

```bash
python evaluate.py \
    --model /workspace/models/groups_model.pt \
    --audio_dir /workspace/bat_data/Pesu/ \
    --output_json /workspace/predictions/pesu_validation.json
```

### 6. Copy outputs out (before destroying container)

Outputs are in `/workspace/output` which is bind-mounted to your host. They're
already on disk outside the container.

## Resuming a training session

### If you're continuing from where the last session left off:

The repo contains the last-trained models in `docker/models/`. These are the
starting point. To pick up and improve:

```bash
# Inside the container
# 1. Extract features for NEW species or NEW recordings only
python extract_features.py --species NewSpecies

# 2. Retrain (it will pick up ALL .npz files in /workspace/features/)
python train_classifier.py --all
```

### If you're adding more Epfu data to fix the partial-extraction issue:

```bash
# 1. Put the new Epfu files into bat_data/Epfu/ (can be mixed with existing)
# 2. Delete the old features file so extraction reruns
rm /workspace/features/Epfu.npz

# 3. Re-extract (only Epfu needs redoing)
python extract_features.py --species Epfu

# 4. Retrain — the other species features are cached, so this only retrains the head
python train_classifier.py --all
```

## Using on Vast.ai specifically

Vast.ai instances come with Docker pre-installed. You can skip the build step
if you use a PyTorch image directly, OR build from source:

```bash
# On a fresh Vast.ai instance (RTX 5090 recommended):
ssh -p <port> root@<ip>
cd /workspace
git clone https://github.com/stafa-san/bat-edge-monitor.git
cd bat-edge-monitor/docker
docker build -t bat-trainer .

# Then upload bat_data/ via rsync from your Mac:
# rsync -avz -e "ssh -p <port>" ~/Downloads/bat_data/ root@<ip>:/workspace/bat_data/

docker run --gpus all \
    -v /workspace/bat_data:/workspace/bat_data \
    -v /workspace/output:/workspace/output \
    -it bat-trainer
```

## Why Docker and not just pip install?

- **Reproducibility** — BatDetect2's dependencies shift over time. Pinning them
  in a Dockerfile means training next year produces the same results as today.
- **Portability** — works on Vast.ai, RunPod, Lambda, or local GPU. No
  per-platform setup.
- **Recovery** — when a Vast.ai instance disappears (hosts leave the market),
  you can spin up a new one and be training within 10 minutes instead of hours.

## Expected performance

| Model | Val accuracy | Val macro F1 |
|-------|--------------|--------------|
| frequency (2-class) | 91-94% | 0.86+ |
| groups (5-class)    | 84-89% | 0.66-0.80 |
| species (9-class)   | 71-76% | 0.49-0.60 |

Groups model is the production target. Species model is expected to do worse
due to Dr. Johnson's "acoustically indistinguishable Myotis" constraint.

## Pi deployment

See `BATDETECT2_TRAINING.md` in repo root for full integration guide.
Short version: load `docker/models/groups_model.pt` in Pi's Python service,
run inference the same way `evaluate.py` does, write predicted class +
confidence to PostgreSQL/Firestore.
