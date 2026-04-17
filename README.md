# 🦇 Soundscape Monitor

Real-time acoustic + environmental monitoring using AudioMoth, HOBO MX2201, and a Raspberry Pi 5, with a North American bat classifier trained on Ohio recordings.

Live dashboard: https://bat-edge-monitor-dashboard.vercel.app/

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       Raspberry Pi 5                          │
│                                                               │
│  ┌──────────┐   ┌─────────────────────────┐   ┌────────────┐ │
│  │ AudioMoth│──▶│ BatDetect2 (frozen) +   │──▶│            │ │
│  │  192kHz  │   │ groups classifier head  │   │            │ │
│  └──────────┘   │ 5 classes: EPFU_LANO,   │   │            │ │
│                 │ LABO, LACI, MYSP, PESU  │   │ PostgreSQL │ │
│                 └─────────────────────────┘   │  (buffer)  │ │
│                 ┌─────────────────────────┐   │            │ │
│                 │ AST (527 AudioSet tags) │──▶│            │ │
│                 └─────────────────────────┘   │            │ │
│  ┌──────────┐   ┌─────────────────────────┐   │            │ │
│  │ HOBO BLE │──▶│ hobo-ble-service        │──▶│            │ │
│  │ MX2201   │   │ (temperature loggers)   │   └─────┬──────┘ │
│  └──────────┘   └─────────────────────────┘         │        │
│                                                     ▼        │
│  POST /wav ──▶ analysis-api ─┐             ┌──────────────┐  │
│  (offline)                   │             │ sync-service │  │
│                              │             │ + watchdog   │  │
│                              └────────────▶│ + OneDrive   │  │
│                                            └──────┬───────┘  │
└───────────────────────────────────────────────────┼──────────┘
                                                    │
                        ┌───────────────────────────┴────────────┐
                        ▼                                        ▼
             ┌───────────────────┐                  ┌──────────────────────┐
             │ Firebase Firestore│                  │ UC OneDrive          │
             │  (metadata)       │                  │ (Tier 1 WAVs)        │
             └─────────┬─────────┘                  └──────────────────────┘
                       ▼
             ┌───────────────────┐
             │ Next.js Dashboard │
             │ (Vercel)          │
             └───────────────────┘
```

## Services (`edge/`)

| Service | Role |
|---------|------|
| `batdetect-service` | Captures AudioMoth at 192 kHz, runs BatDetect2 + the 5-class groups classifier head |
| `ast-service` | Audio Spectrogram Transformer for general soundscape tags |
| `hobo-ble-service` | BLE scanner for HOBO MX2201 temperature loggers |
| `analysis-api` | FastAPI endpoint (port 8080) for offline `.wav` analysis |
| `sync-service` | Pushes Postgres rows to Firestore, enforces disk quota, archives Tier 1 to OneDrive |
| `db` | Postgres 16 buffer so data survives network outages |

## Classifier

Raw BatDetect2 is UK-trained and misclassifies every Ohio bat as European species (Rhinolophus, Pipistrellus, Eptesicus serotinus). We freeze BatDetect2 as a feature extractor and run a trained 5-class classifier head on its 32-dim features.

- **Classes**: `EPFU_LANO, LABO, LACI, MYSP, PESU` (Dr. Joseph Johnson's acoustic grouping — several species are echolocation-indistinguishable)
- **Training**: Vast.ai RTX 5090, 2026-04-17, 279k detections across 9 species
- **Held-out validation**: 80% overall accuracy, **83.4% PESU (up from 0% through raw UK BatDetect2)**
- **Model**: [`docker/models/groups_model.pt`](docker/models/groups_model.pt) — ~13k params, runs on Pi CPU

See [`BATDETECT2_TRAINING.md`](BATDETECT2_TRAINING.md) for the full training run context and reproducibility instructions.

## Data flywheel

Detections land in tiered on-Pi storage; high-confidence rare-species recordings flow to UC OneDrive for long-term archival and advisor review.

| Tier | Trigger | Destination | Retention |
|------|---------|-------------|-----------|
| 1 | Rare class (PESU/LACI/LABO) ≥ 0.9 confidence | UC OneDrive via rclone | Forever |
| 2 | Any detection ≥ 0.5 confidence | Pi SD card | 30 days |
| 3 | Detections but all < 0.5 | Postgres row only, no WAV | n/a |
| 4 | All detections < 0.3 | Pi SD card | 7 days |

A disk watchdog reclaims space under pressure and halts recordings before anything unsynced or human-verified is at risk. Tunable thresholds live at the top of [`edge/batdetect-service/src/storage.py`](edge/batdetect-service/src/storage.py); full architecture in [`DATA_FLYWHEEL.md`](DATA_FLYWHEEL.md).

## Hardware

- Raspberry Pi 5 (4 GB+)
- AudioMoth USB Microphone (firmware v1.3.1, 192 kHz)
- HOBO MX2201 Bluetooth temperature loggers (one or more)
- USB Micro-B → USB-A cable

## Quick start

### Edge (on the Pi)

```bash
cd edge
docker compose up --build
```

All feature flags default off. To activate after verification:

```bash
cat >> edge/.env <<EOF
ENABLE_GROUPS_CLASSIFIER=true
ENABLE_STORAGE_TIERING=true
ENABLE_ONEDRIVE_SYNC=true
EOF
docker compose up -d
```

OneDrive sync requires a one-time OAuth setup per Pi:

```bash
bash edge/scripts/setup_rclone_onedrive.sh
```

### Dashboard (local dev)

```bash
cd dashboard
npm install
npm run dev
```

### Verification scripts

```bash
# Tier logic + disk watchdog + OneDrive orchestration (stdlib only, no deps)
python edge/scripts/verify_storage_tiering.py

# End-to-end classifier pipeline (requires batdetect2==1.3.1 + a sample WAV)
python edge/scripts/verify_classifier_pipeline.py --wav path/to/bat.wav
```

## Research context

Part of the thesis *"Beyond Single Sensors: Quantifying Data Integrity in Multi-modal Edge Systems for Real-Time Ecological Monitoring"* — Mustapha Zakari Nasomah, MS IT, University of Cincinnati, advised by Dr. Joseph S. Johnson.

## References

- Adamiak, M. (2025). *Audio Spectrogram Transformers Beyond the Lab.*
- Mac Aodha, O. et al. (2022). *Towards a General Approach for Bat Echolocation Detection and Classification.*
- Gong, Y. et al. (2021). *AST: Audio Spectrogram Transformer.*

## Links

- Dashboard: https://bat-edge-monitor-dashboard.vercel.app/
- Training documentation: [`BATDETECT2_TRAINING.md`](BATDETECT2_TRAINING.md)
- Data flywheel architecture: [`DATA_FLYWHEEL.md`](DATA_FLYWHEEL.md)
