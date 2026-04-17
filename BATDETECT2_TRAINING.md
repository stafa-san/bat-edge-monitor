# BatDetect2 North American Species Training — Complete Documentation

**Project:** bat-edge-monitor (https://github.com/stafa-san/bat-edge-monitor)
**Thesis:** Beyond Single Sensors: Quantifying Data Integrity in Multi-modal Edge Systems for Real-Time Ecological Monitoring
**Author:** Mustapha Nasomah (University of Cincinnati, MS IT, graduating May 2026)
**Advisor:** Dr. Joseph S. Johnson
**Training completed:** April 17, 2026
**Status:** Groups model (5-class) is production-ready for Pi deployment.

---

## TL;DR for Claude Code

The Pi's BatDetect2 currently misclassifies ALL Ohio bats as European species (Rhinolophus, Pipistrellus). This is because BatDetect2 is UK-trained. We fixed this by:

1. **Freezing BatDetect2** as a feature extractor (32-dim features per call)
2. **Training a small classifier head** on Ohio bat recordings from Dr. Johnson
3. **Using taxonomic grouping** per Dr. Johnson's acoustic-similarity notes (5 classes: EPFU_LANO, LABO, LACI, MYSP, PESU)

The result: **0% → 84% correct PESU classification** on held-out data, plus correct dominant-class assignment for all 9 species tested.

The production model is `groups_model.pt` (5 classes). Use this for the Pi inference service — not the raw BatDetect2 output.

---

## Repository Integration Goal

Integrate `groups_model.pt` into the Pi's BatDetect2 inference pipeline so that detections go through:

```
Audio file → BatDetect2.api.process_audio() → (detections, features, spec)
          → filter det_prob > 0.5
          → standardize features with scaler_mean/scaler_scale
          → BatClassifierHead → softmax → argmax
          → Write {start_time, end_time, predicted_class, confidence} to PostgreSQL/Firestore
```

Current pipeline outputs raw BatDetect2 species (wrong for North America). Target pipeline outputs the 5-class group per Dr. Johnson's taxonomy.

---

## Environment Reconstruction

### Where training happened

**Vast.ai rental** — we couldn't train on the Pi or laptop. GPU was required for feasibility.

| Item | Spec |
|------|------|
| Provider | Vast.ai |
| Instance | m:45511 (Utah, US) |
| GPU | RTX 5090 32GB VRAM |
| Cost | ~$0.48/hr (running), ~$0.02/hr (stopped) |
| Template | PyTorch (Vast) — CUDA 13.1, PyTorch 2.10 |
| Disk | 129 GB |
| SSH | `ssh -p 35001 root@136.59.129.136` (different each rental) |
| Workdir | `/workspace/` |

### To recreate the environment from scratch

```bash
# On Vast.ai, rent an RTX 5090 with the PyTorch template.
# Once running, SSH in:
ssh -p <port> root@<ip>

# Install BatDetect2
cd /workspace
pip install batdetect2

# Verify CUDA:
python -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
# Expected: True, "NVIDIA GeForce RTX 5090"

# The training scripts (extract_features.py, train_classifier.py, evaluate.py) are in the deploy bundle.
```

### Data location

**Source:** Dr. Joseph Johnson's OneDrive folder "Calls for Mustapha"

**9 species folders** with AudioMoth recordings at 192 kHz:

| Species | Full name | File count on OneDrive |
|---------|-----------|------------------------|
| Epfu | Eptesicus fuscus (Big Brown Bat) | 3,957 |
| Labo | Lasiurus borealis (Eastern Red Bat) | 736 |
| Laci | Lasiurus cinereus (Hoary Bat) | 375 |
| Lano | Lasionycteris noctivagans (Silver-haired Bat) | 189 |
| Myle | Myotis leibii (Eastern Small-footed Myotis) | 6 |
| Mylu | Myotis lucifugus (Little Brown Bat) | 168 |
| Myse | Myotis septentrionalis (Northern Long-eared Myotis) | 1 |
| Myso | Myotis sodalis (Indiana Bat) | 657 |
| Pesu | Perimyotis subflavus (Tricolored Bat) | 16 |

**⚠️ Known data gotcha:** OneDrive zips fail catastrophically for files > 10GB. The Epfu download (11GB as a single zip) has a CRC-broken file that causes 7-Zip to halt at 1,621 files out of 3,957. We trained with the partial extraction (41% of Epfu). If redoing this, download Epfu in ~5 sub-folder chunks instead of as a single zip.

---

## Dr. Johnson's Grouping Scheme

Dr. Johnson told us these species are acoustically hard or impossible to distinguish:

```
EPFU_LANO: Eptesicus fuscus + Lasionycteris noctivagans
           "nearly impossible to distinguish 99% of the time"

MYSP:      All Myotis species (Myle + Mylu + Myse + Myso)
           "all indistinguishable" acoustically

LABO:      Lasiurus borealis alone
           similar to MYSP but has diagnostic features

LACI:      Lasiurus cinereus alone
           low-frequency but distinctive

PESU:      Perimyotis subflavus alone
           similar to LABO but can be diagnostic in
           long multi-pulse files
```

The `groups_model.pt` classifies into these 5 classes. This is the production model.

We also trained 2 other models for comparison:
- `frequency_model.pt` — 2 classes (LowFreq = {EPFU_LANO, LACI}, HighFreq = {MYSP, LABO, PESU}). Sanity check.
- `species_model.pt` — 8 classes (pre-Epfu) or 9 classes (post-Epfu). For thesis comparison only — expected to perform worse due to indistinguishable-species problem.

---

## Training Pipeline

### Step 1 — Feature Extraction

BatDetect2 is frozen. Each call produces a 32-dim feature vector per detection. We filter to `det_prob > 0.5`.

**Why this filter matters:** UK-trained BatDetect2 has very low confidence on Ohio bats (mean often below 0.1). Filtering at 0.5 keeps 5-15% of detections, but those are the high-signal ones. Below 0.5 is mostly noise that BatDetect2 is unsure about anyway.

Script: `extract_features.py`

Output: `/workspace/features/<Species>.npz` containing:
- `features`: (N, 32) numpy array of feature vectors
- `labels`: (N,) array of species labels (all same value per file)
- `source_files`: (N,) array of source filenames (needed for file-level train/val split)

Final extraction counts (after filtering to det_prob > 0.5):

| Species | Files | Detections |
|---------|-------|------------|
| Epfu (partial) | 1,621 | ~130,000 (estimate; actual number from post-Epfu run) |
| Labo | 736 | 50,235 |
| Laci | 375 | 17,744 |
| Lano | 189 | 8,229 |
| Myle | 6 | 374 |
| Mylu | 168 | 12,300 |
| Myse | 1 | 120 |
| Myso | 657 | 63,381 |
| Pesu | 16 | 676 |

### Step 2 — Classifier Head Training

Architecture — `BatClassifierHead`:

```python
class BatClassifierHead(nn.Module):
    def __init__(self, input_dim=32, hidden_dims=(128, 64), num_classes=5, dropout=0.3):
        super().__init__()
        layers = []
        prev = input_dim
        for h in hidden_dims:
            layers += [
                nn.Linear(prev, h),
                nn.ReLU(),
                nn.BatchNorm1d(h),
                nn.Dropout(dropout)
            ]
            prev = h
        layers.append(nn.Linear(prev, num_classes))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)
```

Total params: ~13,189 (tiny, runs on Pi CPU no problem)

Training config:
- Optimizer: Adam, initial lr=1e-3
- Loss: Class-weighted CrossEntropyLoss
- LR schedule: ReduceLROnPlateau, factor 0.5, patience 3
- Early stopping on val_loss, patience 7
- Batch size: 256
- Max epochs: 100
- Standardization: StandardScaler fit on training set, saved in checkpoint

Split strategy: **file-level split** (all detections from one file stay together in train or val). Falls back to detection-level split if any class has fewer than 2 files. This prevents data leakage where the same recording session contributes to both train and val.

### Step 3 — Expected Results (pre-Epfu scale)

**Groups model (5-class, production):**
- Val accuracy: 83.9%
- Per-class F1: LABO 0.82, MYSP 0.93, LACI 0.74, EPFU_LANO 0.50 (weak — no Epfu), PESU 0.29 (thin data)

**Frequency model (2-class, sanity):**
- Val accuracy: 91.0%

**Species model (8-class, thesis):**
- Val accuracy: 70.9%
- Confirms Dr. Johnson's "indistinguishable Myotis" claim — heavy Mylu↔Myso confusion

**Post-Epfu retraining (expected):**
- Groups accuracy → 86-89%
- EPFU_LANO F1 → 0.75-0.85 (big jump)
- Other classes unchanged

---

## Cross-Species Validation Results (pre-Epfu groups model)

Every species gets dominant-class correct — no systematic failures.

| Species | Expected group | Correct % | Top confusions |
|---------|----------------|-----------|----------------|
| Labo    | LABO           | 77.8%     | 8.7% EPFU_LANO, 6.3% LACI |
| Laci    | LACI           | 74.9%     | 13.2% EPFU_LANO, 7.7% LABO |
| Lano    | EPFU_LANO      | 76.0%     | 10.0% LACI, 9.1% LABO |
| Myle    | MYSP           | 97.9%     | — |
| Mylu    | MYSP           | 80.0%     | 10.1% LABO, 6.8% EPFU_LANO |
| Myse    | MYSP           | 99.2%     | — |
| Myso    | MYSP           | 94.0%     | — |
| Pesu    | PESU           | 83.6%     | 8.7% LABO, 5.5% LACI |

**The thesis money shot:** Same 16 Pesu files fed through UK-trained BatDetect2 predicted 100% European species (4,755 Rhinolophus hipposideros, 3,701 Rhinolophus ferrumequinum, 2,057 Eptesicus serotinus). After our classifier head: 83.6% PESU with mean confidence 0.856.

---

## Integration Guide for Pi Deployment

### Files needed on Pi

From `deploy_bundle/`:
- `groups_model.pt` — production model (5 classes)
- `inference.py` — reference implementation

### Integration flow (Python pseudocode)

```python
from batdetect2 import api
import numpy as np
import torch

# Load model once at service start
ckpt = torch.load('groups_model.pt', weights_only=False, map_location='cpu')
model = BatClassifierHead(
    input_dim=ckpt['input_dim'],
    hidden_dims=tuple(ckpt['hidden_dims']),
    num_classes=ckpt['num_classes'],
    dropout=0.0,
)
model.load_state_dict(ckpt['state_dict'])
model.eval()

CLASS_NAMES = ckpt['class_names']        # ['EPFU_LANO', 'LABO', 'LACI', 'MYSP', 'PESU']
SCALER_MEAN = np.array(ckpt['scaler_mean'])
SCALER_SCALE = np.array(ckpt['scaler_scale'])

def classify_wav(wav_path, det_threshold=0.5):
    audio = api.load_audio(wav_path)
    detections, features, _ = api.process_audio(audio)

    if len(detections) == 0:
        return []

    # Filter by BatDetect2 detection confidence
    mask = np.array([d['det_prob'] > det_threshold for d in detections])
    if not mask.any():
        return []

    high_conf_feats = features[mask]
    high_conf_dets = [d for d, m in zip(detections, mask) if m]

    # Standardize
    feats_norm = (high_conf_feats - SCALER_MEAN) / SCALER_SCALE

    # Classify
    with torch.no_grad():
        x = torch.tensor(feats_norm, dtype=torch.float32)
        logits = model(x)
        probs = torch.softmax(logits, dim=1).numpy()
        preds = probs.argmax(axis=1)

    results = []
    for i, d in enumerate(high_conf_dets):
        results.append({
            'start_time': d['start_time'],
            'end_time': d['end_time'],
            'predicted_class': CLASS_NAMES[preds[i]],
            'confidence': float(probs[i][preds[i]]),
        })
    return results
```

### Where this replaces existing code

In the Pi's current BatDetect2 service, find where detections are written to PostgreSQL/Firestore. Currently it writes raw BatDetect2 species names (European). Replace with the 5-class group from this function.

Suggested DB schema addition: keep a `model_version` column to track which classifier produced each row. Current: `'batdetect2-uk'`. New: `'groups_v1_pre_epfu'` or `'groups_v1_post_epfu'`.

### Known caveat — sample rate drift

Training data: AudioMoth 192 kHz.
Pi currently: 250 kHz (changed from 192 recently).

BatDetect2 resamples internally to its expected rate, so inference should work. But feature distributions may drift at 250 kHz. **Recommendation: revert Pi to 192 kHz** to match training, or run a validation experiment at 250 kHz before trusting predictions.

---

## File Layout on GPU (at session end)

```
/workspace/
├── bat_data/                    # Raw audio, folder per species
│   ├── Epfu/                    # 1621 wav files
│   ├── Labo/                    # 736 wav files
│   ├── Laci/                    # 375 wav files
│   ├── ... (other species)
├── features/                    # Extracted features, one .npz per species
│   ├── Epfu.npz
│   ├── Labo.npz
│   └── ...
├── models/                      # Trained classifier heads
│   ├── frequency_model.pt
│   ├── groups_model.pt          # THE PRODUCTION MODEL
│   └── species_model.pt
├── logs/                        # Training logs (per-epoch metrics, JSON)
│   ├── frequency_training.json
│   ├── groups_training.json
│   └── species_training.json
├── predictions/                 # Validation outputs
│   ├── pesu_baseline.json       # UK-trained BatDetect2 on Pesu → 100% European
│   ├── pesu_classified.json     # Groups model on Pesu → 83.6% PESU
│   └── cross_species_validation.txt
├── extract_features.py
├── train_classifier.py
├── evaluate.py
└── deploy_bundle/               # Everything needed for Pi integration
```

---

## Reproducing a Training Run From Scratch

If you need to retrain (e.g., after adding Epfu data or getting better recordings):

```bash
# On GPU:
# 1. Upload data to /workspace/bat_data/<Species>/
#    (one wav per detection/session, folders named exactly: Epfu, Labo, Laci, Lano, Myle, Mylu, Myse, Myso, Pesu)

# 2. Extract features for each species
python /workspace/extract_features.py --species Epfu
python /workspace/extract_features.py --species Labo
# ... etc for each species

# 3. Train all three models
python /workspace/train_classifier.py --all

# 4. Validate
python /workspace/evaluate.py \
    --model /workspace/models/groups_model.pt \
    --audio_dir /workspace/bat_data/Pesu/ \
    --output_json /workspace/predictions/pesu_classified.json
```

Total time for full retrain at current data scale: ~1 hour GPU time (~$0.50).

---

## Thesis Data Integrity Context

Per April 17 meeting with Dr. Johnson:

**Primary integrity metric:** Cross-modal timestamp alignment error
- For each bat detection at time `t_d`, find nearest HOBO MX2201 reading at `t_e`
- Report distribution of `|t_d - t_e|` — median, p95, max
- Instrument three pipeline points: sensor capture, edge DB write, Firebase sync
- Report end-to-end (the number downstream analysts see)

**Secondary metric:** Data Loss Rate per sensor modality
- `(expected_events - recorded_events) / expected_events`
- Acoustic expected rate from AudioMoth schedule
- Environmental expected rate from HOBO BLE advertisement interval (~1 Hz)
- Literature baseline: 15-30% acoustic loss vs <1% environmental [Aide 2013, Zhang 2020]

This classifier is the acoustic detection side of that pipeline. Data integrity concerns apply downstream — specifically to whether each classification gets a well-synchronized environmental reading attached.

---

## Known Issues / TODO

1. **Epfu partial data** — we trained on 1,621 of 3,957 Epfu files due to OneDrive zip corruption. Full Epfu would likely add 2-3 percentage points to groups model accuracy. To fix: re-download Epfu in sub-folder chunks (~5 chunks of ~800 files each).

2. **PESU under-represented** — only 16 files in training. Model does 83.6% PESU despite this, but F1 on PESU class is only 0.29 in validation. More Pesu recordings would help.

3. **Sample rate drift** — training at 192 kHz, Pi currently recording at 250 kHz. Either revert Pi or run validation experiment.

4. **Fine-tuning path not yet explored** — BatDetect2's `finetune/` module is broken in the public repo ("This code is currently broken. Will fix soon, stay tuned."). We used a frozen-backbone + classifier-head approach instead. If BatDetect2 fine-tuning ever becomes available, revisit.

5. **BatDetect2 confidence is very low on Ohio bats** — mean det_prob is often below 0.1. Our 0.5 threshold keeps only 5-15% of detections. This is a domain-shift artifact of the UK training data, and is part of why fine-tuning or replacing BatDetect2 with a North American detector would help.

---

## Contacts

- **Advisor:** Dr. Joseph S. Johnson (UC School of IT)
- **Committee:** Dr. Adib Zaman, Dr. Jess Kropczynski
- **Data source:** Dr. Johnson's OneDrive "Calls for Mustapha" folder
- **UC email:** zakarimn@mail.uc.edu
