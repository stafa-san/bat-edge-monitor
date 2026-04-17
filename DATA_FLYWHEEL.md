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

The thresholds below are the **defaults shipped in `edge/batdetect-service/src/storage.py`** (last tuned 2026-04-17). See the [Tuning](#tuning-the-thresholds) section at the bottom for how to change them.

### Tier 1: Save forever (high-confidence + rare species)
- Any detection with predicted class in `{PESU, LACI, LABO}` AND `prediction_confidence >= 0.9`
- On-Pi path: `/bat_audio/tier1_permanent/<predicted_class>/<site>_<timestamp>.wav`
- Multi-class files go to the rarest class present (priority: PESU > LACI > LABO > MYSP > EPFU_LANO)
- Full 192 kHz 16-bit WAV, no compression
- Uploaded to UC OneDrive; local copy deleted after upload confirms

### Tier 2: Save for 30 days (medium confidence — might be useful)
- Any detection with `prediction_confidence >= 0.5` that didn't qualify for tier 1
- On-Pi path: `/bat_audio/tier2_30day/<site>_<timestamp>.wav` (flat, no species subfolder)
- Local only; no OneDrive upload; `expires_at = detection_time + 30d`
- Disk watchdog deletes expired files; may pre-emptively delete under disk pressure

### Tier 3: Save metadata only (low confidence — probably noise)
- Detections exist but every one falls below 0.5 confidence (and above 0.3)
- Store the detection row in Postgres with `audio_path = NULL, storage_tier = 3`
- Audio file is **never written to disk** — decision is made before the WAV is archived

### Tier 4: Anomaly recordings (unknown audio worth investigating)
- Every detection below `TIER4_CONFIDENCE_MAX` (0.3) — could be new species or corrupted recording
- On-Pi path: `/bat_audio/tier4_anomaly/<site>_<timestamp>.wav` (flat)
- Kept 7 days for manual review, then reclaimed by the disk watchdog
- NOTE: the "no detections at all" case from earlier drafts of this doc is **not** archived today — the Pi service only runs tiering when BatDetect2 returns detections. Adding an "entirely silent segment" capture would require a schema change to allow detection-less rows. Out of scope for Stage D1.

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

## Tuning the thresholds

All tunable knobs live at the top of [`edge/batdetect-service/src/storage.py`](edge/batdetect-service/src/storage.py). No other file reads these values — edit, rebuild the `batdetect-service` image, restart.

```python
THRESHOLDS_LAST_TUNED = "2026-04-17"

TIER1_CONFIDENCE_MIN = 0.9     # rare-species -> permanent
TIER2_CONFIDENCE_MIN = 0.5     # any class -> 30-day retention
TIER4_CONFIDENCE_MAX = 0.3     # all detections below this -> anomaly

CLASS_PRIORITY_ORDER = ("PESU", "LACI", "LABO", "MYSP", "EPFU_LANO")
RARE_CLASSES   = {"PESU", "LACI", "LABO"}
COMMON_CLASSES = {"EPFU_LANO", "MYSP"}

TIER2_RETENTION = timedelta(days=30)
TIER4_RETENTION = timedelta(days=7)
```

**Workflow for Dr. Johnson:**

1. Edit the constants in `storage.py`.
2. Bump `THRESHOLDS_LAST_TUNED` so downstream reports can correlate data with the rules that produced it.
3. Run the test suite: `python edge/scripts/verify_storage_tiering.py` — catches obvious bugs in the logic.
4. Rebuild the container on the Pi: `docker compose build batdetect-service && docker compose up -d batdetect-service`.

Thresholds only affect *new* captures. Existing files on disk keep whatever tier they were assigned at capture time.

## Why this matters for your thesis

Add this to your "Expected Contributions" section:

> **Continuous Learning Framework:** A novel approach to edge-deployed
> ecological monitoring that captures field data for iterative model
> improvement. Unlike traditional systems that deploy a fixed model,
> this architecture creates a feedback loop where deployed inference
> produces annotated training data, enabling the model to improve
> over time in ways calibrated to local ecosystems.

This ties together your data integrity metric (alignment error) AND the classifier (groups model) AND the edge architecture (K3S/Pi). It's a compelling narrative.
