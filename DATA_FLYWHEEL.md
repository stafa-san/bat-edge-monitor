# Production Data Flywheel — Capture + Retrain Loop

Goal: Every bat detection the Pi makes in production becomes potential future training data. Over time, your model gets better from your own deployment.

## The architecture

```
┌──────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  AudioMoth   │─────▶│  Pi edge service │─────▶│ PostgreSQL +    │
│  192 kHz     │      │  BatDetect2      │      │ Firestore       │
│              │      │  + groups_model  │      │ (metadata)      │
└──────────────┘      └─────────┬────────┘      └─────────────────┘
                                │
                                ▼
                      ┌──────────────────────┐
                      │  Tiered audio storage│
                      │  (see below)         │
                      └──────────────────────┘
```

## What to save — the tiering problem

You CAN'T save everything — an AudioMoth recording at 192 kHz generates ~1.4 GB per hour. A full deployment season = terabytes. So you triage.

### Tier 1: Save forever (high-confidence + rare species)
- Any detection with `confidence > 0.9` for a rare class (PESU, LACI, LABO)
- Any file where MYSP recall is needed for endangered-species work
- Full 192kHz 16-bit WAV, no compression

### Tier 2: Save for 30 days (medium confidence — might be useful)
- Detections with `confidence 0.5-0.9`
- The full WAV file snippet ±5 seconds around the detection
- After 30 days, delete if not flagged for review

### Tier 3: Save metadata only (low confidence — probably noise)
- Detections with `confidence < 0.5`
- Save just: timestamp, predicted class, confidence, file path (if file retained)
- Audio discarded after 24 hours

### Tier 4: Anomaly recordings (unknown audio worth investigating)
- Files where ALL detections are < 0.3 confidence (could be new species or corrupted recording)
- Files with unusual acoustic features (very loud, very quiet, unusual spectrum)
- Keep 7 days for manual review

## Storage options

| Option | Cost | Capacity | Ease |
|--------|------|----------|------|
| Local microSD on Pi | Free | Limited (256 GB) | Easy |
| External USB drive | ~$100 | 1-4 TB | Easy |
| Firebase Storage | ~$0.026/GB/month | Unlimited | Medium |
| Google Drive (shared with advisor) | $10/month for 2TB | 2 TB | Medium |
| UC research storage (Box, OneDrive) | Free | Varies | Check IT |
| Self-hosted S3 (minio on a NAS) | Hardware cost | Unlimited | Hard |

**My recommendation for a grad-student budget:**
- Tier 1 (forever) → Firebase Storage (small amount, cheap)
- Tier 2 (30 days) → Local USB drive on Pi
- Tier 3/4 → Local, ephemeral
- Also: weekly rsync the USB drive to your UC OneDrive or home NAS

## Database schema addition

Add this to your PostgreSQL/Firestore schema:

```sql
CREATE TABLE detections (
    id BIGSERIAL PRIMARY KEY,
    timestamp_utc TIMESTAMPTZ NOT NULL,
    source_file TEXT NOT NULL,          -- path to WAV if still exists, NULL if discarded
    start_time_in_file DOUBLE PRECISION,
    end_time_in_file DOUBLE PRECISION,
    detection_confidence REAL,           -- BatDetect2 det_prob
    predicted_class TEXT NOT NULL,       -- EPFU_LANO, LABO, LACI, MYSP, PESU
    prediction_confidence REAL NOT NULL, -- softmax of our classifier
    model_version TEXT NOT NULL,         -- e.g., 'groups_v1_post_epfu_2026-04-17'

    -- Human feedback (for future training curation)
    reviewed_by TEXT,                    -- who verified (advisor, self, etc.)
    reviewed_at TIMESTAMPTZ,
    verified_class TEXT,                 -- corrected label if any
    reviewer_notes TEXT,

    -- Environmental context (from HOBO sensor)
    temperature_c REAL,
    humidity_pct REAL,
    temperature_timestamp TIMESTAMPTZ,   -- for thesis integrity metric
    alignment_error_ms REAL,             -- |detection_timestamp - temperature_timestamp|

    -- Storage tiering
    storage_tier INT NOT NULL,           -- 1, 2, 3, or 4
    expires_at TIMESTAMPTZ                -- when to delete the WAV
);

CREATE INDEX ix_detections_class ON detections(predicted_class);
CREATE INDEX ix_detections_time ON detections(timestamp_utc);
CREATE INDEX ix_detections_unverified ON detections(verified_class)
    WHERE verified_class IS NULL;
```

The `verified_class` column is key for the flywheel — when you or Dr. Johnson reviews a recording and corrects the label, that becomes training data.

## Weekly/monthly retraining loop

Once a month (or whenever you have ≥500 new verified labels):

```
1. Export detections where verified_class IS NOT NULL to new WAV folders
   organized by verified_class:
     new_data/Epfu/
     new_data/Labo/
     etc.

2. Copy new WAVs into the training dataset:
     bat_data/Epfu/ <- append new files

3. Spin up a Vast.ai instance, rsync bat_data/, run the Docker container:
     docker run --gpus all -v $(pwd)/bat_data:/workspace/bat_data bat-trainer

4. Inside the container:
     python extract_features.py --species Epfu  # only redo changed species
     python train_classifier.py --all

5. Copy new groups_model.pt back to repo, git commit, deploy to Pi.
```

This is the data flywheel. Your model gets incrementally better from field data.

## Pi-side implementation sketch

```python
# In the Pi's BatDetect2 service, after running the classifier:

import shutil
from pathlib import Path
from datetime import datetime, timedelta

RECORDINGS_DIR = Path("/data/recordings")
TIER1_DIR = RECORDINGS_DIR / "tier1_permanent"  # forever
TIER2_DIR = RECORDINGS_DIR / "tier2_30day"      # 30 days
TIER4_DIR = RECORDINGS_DIR / "tier4_anomaly"    # 7 days, manual review

def determine_tier(predictions):
    """Return storage tier 1-4 based on what the classifier said."""
    if not predictions:
        return 4  # anomaly — nothing detected

    rare_classes = {'PESU', 'LACI', 'LABO'}
    high_conf = [p for p in predictions if p['confidence'] > 0.9]

    # Tier 1: high-confidence rare species
    if any(p['predicted_class'] in rare_classes and p['confidence'] > 0.9
           for p in predictions):
        return 1

    # Tier 4: all detections were low-confidence (could be a new species)
    if all(p['confidence'] < 0.3 for p in predictions):
        return 4

    # Tier 2: medium-confidence anything
    if any(0.5 <= p['confidence'] <= 0.9 for p in predictions):
        return 2

    # Tier 3: everything else (metadata-only)
    return 3

def archive_recording(wav_path: Path, tier: int):
    """Move the WAV to tier storage, compute expiry."""
    if tier == 1:
        dest = TIER1_DIR / wav_path.name
        expires = None  # forever
    elif tier == 2:
        dest = TIER2_DIR / wav_path.name
        expires = datetime.utcnow() + timedelta(days=30)
    elif tier == 4:
        dest = TIER4_DIR / wav_path.name
        expires = datetime.utcnow() + timedelta(days=7)
    else:  # tier 3
        dest = None
        expires = datetime.utcnow() + timedelta(hours=24)
        # Optionally keep 24h and then delete
        shutil.move(str(wav_path), f"/tmp/{wav_path.name}")
        return None, expires

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(wav_path), str(dest))
    return dest, expires

# In main detection loop:
predictions = classify_wav(wav_path)
tier = determine_tier(predictions)
archived_path, expires_at = archive_recording(wav_path, tier)

# Write DB rows
for pred in predictions:
    db.insert({
        'timestamp_utc': extract_timestamp_from_filename(wav_path),
        'source_file': str(archived_path) if archived_path else None,
        'start_time_in_file': pred['start_time'],
        'end_time_in_file': pred['end_time'],
        'predicted_class': pred['predicted_class'],
        'prediction_confidence': pred['confidence'],
        'model_version': 'groups_v1_post_epfu_2026-04-17',
        'storage_tier': tier,
        'expires_at': expires_at,
        # ... plus HOBO data
    })

# Nightly cron: delete expired files
# DELETE FROM detections WHERE expires_at < NOW() AND source_file IS NOT NULL;
# Also: find files in tier2/tier4 with no DB row referencing them, delete those
```

## Manual review workflow for the advisor

To enable Dr. Johnson to curate data easily, build a simple web page that:

1. Queries detections where `verified_class IS NULL` and `prediction_confidence < 0.7`
2. Shows the WAV player, spectrogram, predicted class, Pi timestamp
3. Lets him click: ✓ Correct | ✗ Wrong (dropdown for real class) | 🗑 Noise (skip)
4. Updates `verified_class` in the DB

Your dashboard at `bat-edge-monitor-dashboard.vercel.app` is a good place to add this review UI.

## Cost estimates

Assuming 10 bat calls/hour average, 8 hours/night, 6 months/year = 14,400 calls/year:
- Tier 1 (10% rare): 1,440 recordings × 1MB = 1.4 GB forever = ~$0.04/month in Firebase
- Tier 2 (50%): 7,200 × 1MB × 30 days rolling = ~7 GB rolling = local storage only
- Tier 3 (40%): metadata only, negligible storage
- Tier 4 (~1% anomalies): ~150 × 1MB × 7 days = negligible

**Total cloud cost: <$1/month.** Plus a $100 external drive once.

## Why this matters for your thesis

Add this to your "Expected Contributions" section:

> **Continuous Learning Framework:** A novel approach to edge-deployed
> ecological monitoring that captures field data for iterative model
> improvement. Unlike traditional systems that deploy a fixed model,
> this architecture creates a feedback loop where deployed inference
> produces annotated training data, enabling the model to improve
> over time in ways calibrated to local ecosystems.

This ties together your data integrity metric (alignment error) AND the classifier (groups model) AND the edge architecture (K3S/Pi). It's a compelling narrative.
