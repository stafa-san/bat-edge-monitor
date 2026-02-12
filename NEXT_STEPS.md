# Soundscape Monitor — Next Steps: Pi-to-Firebase-to-Dashboard Pipeline

## Current Status (as of Feb 12, 2026)

### What's Working
- **AudioMoth USB Microphone**: Flashed with firmware v1.3.1, configured at 192kHz, connected to Raspberry Pi 5
- **AST Pipeline (old setup)**: Running at `~/soundscape-monitor/` as a single Docker container, has collected 5,550+ classifications over 2 hours in local PostgreSQL
- **GitHub Repo**: `https://github.com/stafa-san/bat-edge-monitor` — contains full project scaffold with edge services and dashboard
- **Vercel Dashboard**: Deployed at `bat-edge-monitor-dashboard.vercel.app` — live but showing "Waiting for data..." because no data is flowing to Firebase yet
- **Firebase Project**: `bat-edge-monitor` (Project ID: `bat-edge-monitor`) — Firestore database created in test mode

### What Needs to Happen Next
The dashboard is deployed but empty. We need to:
1. Set up Firebase credentials on the Pi
2. Stop the old single-container setup
3. Start the new multi-service Docker stack (AST + BatDetect2 + Sync + PostgreSQL)
4. Verify data flows: Pi → PostgreSQL → Sync Service → Firebase Firestore → Vercel Dashboard

---

## Step-by-Step Instructions

### Step 1: Download Firebase Service Account Key

This key allows the sync service running on the Pi to write data to Firestore.

1. Go to [Firebase Console](https://console.firebase.google.com) → select `bat-edge-monitor` project
2. Click **Project Settings** (gear icon top-left)
3. Click the **Service accounts** tab
4. Click **Generate new private key**
5. Save the downloaded JSON file (it will be named something like `bat-edge-monitor-firebase-adminsdk-xxxxx-xxxxxxxxxx.json`)
6. This file is already gitignored (the `.gitignore` includes `*-firebase-adminsdk-*.json` and `serviceAccountKey.json`)

### Step 2: Place the Key on the Pi

```bash
# The downloaded file is likely in ~/Downloads/
# Copy it to the sync-service directory with the expected name
cp ~/Downloads/bat-edge-monitor-firebase-adminsdk-*.json ~/bat-edge-monitor/edge/sync-service/serviceAccountKey.json

# Verify it's there
ls -la ~/bat-edge-monitor/edge/sync-service/serviceAccountKey.json
```

### Step 3: Create the Edge Environment File

```bash
echo "FIREBASE_PROJECT_ID=bat-edge-monitor" > ~/bat-edge-monitor/edge/.env
```

### Step 4: Stop the Old Single-Container Setup

The old setup at `~/soundscape-monitor/` is still running and using the AudioMoth device. We need to stop it before starting the new stack to avoid device contention.

```bash
cd ~/soundscape-monitor
docker compose down
```

Verify it's stopped:
```bash
docker compose ps
# Should show no running containers
```

### Step 5: Build and Start the New Multi-Service Stack

```bash
cd ~/bat-edge-monitor/edge
docker compose up --build
```

**Expected build times on Raspberry Pi 5:**
- `ast-service`: ~4-5 minutes (PyTorch, transformers, librosa)
- `batdetect-service`: ~5-8 minutes (PyTorch + batdetect2 model)
- `sync-service`: ~1-2 minutes (firebase-admin, psycopg2)
- `db` (PostgreSQL): ~30 seconds (pulls image)

**Expected startup sequence:**
1. PostgreSQL starts first (healthcheck ensures it's ready)
2. AST service starts, downloads AST model from HuggingFace (~300 MB on first run)
3. BatDetect2 service starts, loads bat detection model
4. Sync service starts, connects to both PostgreSQL and Firebase

### Step 6: Verify Each Service is Running

In a separate terminal:
```bash
cd ~/bat-edge-monitor/edge

# Check all containers are up
docker compose ps

# Check AST service logs (should show classification output)
docker compose logs --tail 20 ast-service

# Check BatDetect2 service logs
docker compose logs --tail 20 batdetect-service

# Check sync service logs (should show "Synced X classifications, Y bat detections")
docker compose logs --tail 20 sync-service

# Check database has data
docker compose exec db psql -U postgres -d soundscape -c "SELECT COUNT(*) FROM classifications;"
docker compose exec db psql -U postgres -d soundscape -c "SELECT COUNT(*) FROM bat_detections;"
```

### Step 7: Verify Data in Firebase Firestore

1. Go to [Firebase Console](https://console.firebase.google.com) → `bat-edge-monitor` → Firestore Database
2. You should see two collections appearing:
   - `classifications` — with documents containing label, score, spl, device, syncTime
   - `batDetections` — with documents containing species, detectionProb, lowFreq, highFreq, durationMs

### Step 8: Verify the Dashboard

1. Go to `https://bat-edge-monitor-dashboard.vercel.app`
2. The status indicator should change from "Connecting..." to "Live" (green dot)
3. Stats cards should populate with counts
4. Sound Class Distribution chart should show bars
5. Bat Detection Feed will populate when bats are detected (unlikely indoors, but the component is ready)
6. SPL Timeline should show dB readings over time

---

## Architecture Reference

### Docker Compose Services (edge/docker-compose.yml)

```
┌─────────────────────────────────────────────────────────────┐
│ docker compose (edge/)                                       │
│                                                              │
│  ┌──────────────┐  ┌──────────────────┐  ┌───────────────┐ │
│  │  ast-service  │  │ batdetect-service│  │ sync-service  │ │
│  │  (python 3.11)│  │  (python 3.10)   │  │ (python 3.11) │ │
│  │  Port: none   │  │  Port: none      │  │ Port: none    │ │
│  │  /dev/snd ✓   │  │  /dev/snd ✓      │  │ No audio      │ │
│  └──────┬────────┘  └────────┬─────────┘  └──────┬────────┘ │
│         │                    │                    │          │
│         ▼                    ▼                    │          │
│  ┌──────────────────────────────────────┐        │          │
│  │          PostgreSQL 16 (db)          │◄───────┘          │
│  │          Port: 5432                  │                    │
│  │          Volume: pgdata              │                    │
│  └──────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **AudioMoth** captures audio at 192kHz via USB
2. **AST Service** records 1-second samples, downsamples to 16kHz, classifies using Audio Spectrogram Transformer (527 categories), calculates SPL, writes top 5 labels to `classifications` table
3. **BatDetect2 Service** records 5-second segments at full 192kHz, runs bat echolocation detection, writes detected bat calls with species and call parameters to `bat_detections` table
4. **Sync Service** (every 60 seconds) reads unsynced rows from both tables, batch-writes to Firebase Firestore, marks as synced
5. **Vercel Dashboard** has real-time Firestore `onSnapshot` listeners that update the UI instantly when new data arrives

### Database Schema

**classifications table** (AST model output):
| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PK | Auto-increment ID |
| label | VARCHAR(255) | Sound class (e.g., "Speech", "Bird", "Static") |
| score | FLOAT | Sigmoid confidence score (0.0–1.0) |
| spl | FLOAT | Sound pressure level in dB |
| device | VARCHAR(100) | Device identifier ("AudioMoth") |
| sync_id | UUID | Groups the 5 labels from one sample |
| sync_time | TIMESTAMP | When the classification was made |
| synced | BOOLEAN | Whether it's been pushed to Firestore |

**bat_detections table** (BatDetect2 output):
| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PK | Auto-increment ID |
| species | VARCHAR(255) | Bat species name |
| common_name | VARCHAR(255) | Common name |
| detection_prob | FLOAT | Detection probability (0.0–1.0) |
| start_time | FLOAT | Call start within segment (seconds) |
| end_time | FLOAT | Call end within segment (seconds) |
| low_freq | FLOAT | Lowest frequency of call (Hz) |
| high_freq | FLOAT | Highest frequency of call (Hz) |
| duration_ms | FLOAT | Call duration in milliseconds |
| device | VARCHAR(100) | Device identifier |
| sync_id | UUID | Groups detections from one segment |
| detection_time | TIMESTAMP | When the detection was made |
| synced | BOOLEAN | Whether it's been pushed to Firestore |

### Firestore Collections

**`classifications`** — mirrors the PostgreSQL table (camelCase fields):
```json
{
  "label": "Speech",
  "score": 0.045,
  "spl": 33.3,
  "device": "AudioMoth",
  "syncId": "uuid-string",
  "syncTime": "Timestamp",
  "createdAt": "SERVER_TIMESTAMP"
}
```

**`batDetections`** — mirrors the bat_detections table:
```json
{
  "species": "Pipistrellus pipistrellus",
  "commonName": "Common Pipistrelle",
  "detectionProb": 0.87,
  "startTime": 1.23,
  "endTime": 1.28,
  "lowFreq": 42000,
  "highFreq": 78000,
  "durationMs": 50,
  "device": "AudioMoth",
  "syncId": "uuid-string",
  "detectionTime": "Timestamp",
  "createdAt": "SERVER_TIMESTAMP"
}
```

---

## Troubleshooting

### "Audio device not found" in AST or BatDetect2 service
- Check AudioMoth is plugged in with switch on DEFAULT
- Run `arecord -l` on the Pi (outside Docker) to confirm device is detected
- The card number might change after reboot. The code auto-detects by name ("AudioMoth") so this should be handled.

### "Audio device busy" — both services trying to record simultaneously
- The `plughw` interface should allow shared access
- If it doesn't work, we need to implement a shared audio capture service that distributes samples to both classifiers
- Quick fix: add a small delay (`capture_delay=1`) to stagger recordings

### Sync service can't connect to Firebase
- Verify `serviceAccountKey.json` exists at `edge/sync-service/serviceAccountKey.json`
- Verify it's a valid JSON file: `cat edge/sync-service/serviceAccountKey.json | python3 -m json.tool`
- Check the Firestore database is in test mode (Firebase Console → Firestore → Rules tab)

### Dashboard shows "Connecting..." but never "Live"
- Check Firebase env vars are set correctly in Vercel (Settings → Environment Variables)
- The `NEXT_PUBLIC_FIREBASE_PROJECT_ID` must match exactly: `bat-edge-monitor`
- Check browser console (F12) for Firebase connection errors

### BatDetect2 not detecting any bats
- This is expected indoors! Bat echolocation calls are ultrasonic (20–120 kHz) and only occur outdoors at night
- The model is trained on UK species; North American species may need fine-tuning
- To test the model works, you can play back a bat call recording through speakers (must be ultrasonic-capable) or process a sample .wav file from the batdetect2 example_data

### Build fails on Pi
- Ensure Docker has enough disk space: `df -h`
- The Pi 5 with 4GB RAM may run tight during builds. If OOM killed, try building one service at a time: `docker compose build ast-service`
- Check Docker is using the 64-bit ARM images: `uname -m` should show `aarch64`

---

## Known Issues

1. **SPL divide-by-zero warnings**: Fixed in the new code with epsilon, but the old `~/soundscape-monitor` setup still shows them
2. **Docker Compose `version` warning**: Remove the `version` key from any docker-compose.yml to suppress
3. **BatDetect2 Python 3.10 requirement**: Uses a separate container with Python 3.10 since batdetect2 requires >=3.8, <3.11
4. **AudioMoth Flash App**: Does not run on ARM64 — firmware flashing must be done from macOS/Windows (one-time operation, already completed)
5. **Firestore test mode expires in 30 days**: Need to update security rules before expiration

---

## Future Enhancements

1. **ONNX quantized AST model** for faster inference on Pi
2. **Shared audio capture service** if device contention becomes an issue
3. **Historical analytics page** on the dashboard (hourly/daily aggregations)
4. **Device health monitoring** (uptime, capture failures, model latency)
5. **Multi-device support** (multiple Pi + AudioMoth nodes syncing to same Firestore)
6. **BatDetect2 fine-tuning** for North American bat species
7. **Firestore security rules** for production deployment
8. **Authentication** on the dashboard (Firebase Auth)

---

## Environment Variables Reference

### Edge (edge/.env)
```
FIREBASE_PROJECT_ID=bat-edge-monitor
```

### Dashboard (dashboard/.env.local)
```
NEXT_PUBLIC_FIREBASE_API_KEY=your-api-key
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=bat-edge-monitor.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=bat-edge-monitor
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=bat-edge-monitor.firebasestorage.app
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=364680758702
NEXT_PUBLIC_FIREBASE_APP_ID=your-app-id
```

### Docker Compose Environment (set in docker-compose.yml)
```
DB_HOST=db
DB_NAME=soundscape
DB_USER=postgres
DB_PASSWORD=changeme
DEVICE_NAME=AudioMoth
SAMPLE_RATE=192000
DETECTION_THRESHOLD=0.3  (batdetect-service only)
SEGMENT_DURATION=5       (batdetect-service only)
SYNC_INTERVAL=60         (sync-service only)
```

---

## File Locations on Raspberry Pi

| Path | Description |
|------|-------------|
| `~/bat-edge-monitor/` | Main project repo (GitHub) |
| `~/bat-edge-monitor/edge/` | All Pi-side Docker services |
| `~/bat-edge-monitor/edge/sync-service/serviceAccountKey.json` | Firebase admin key (gitignored) |
| `~/bat-edge-monitor/edge/.env` | Firebase project ID |
| `~/bat-edge-monitor/dashboard/` | Next.js dashboard (deployed to Vercel) |
| `~/soundscape-monitor/` | OLD single-container setup (can be removed after migration) |

## Hardware Reference

| Component | Details |
|-----------|---------|
| Raspberry Pi 5 | 4GB RAM, Raspberry Pi OS 64-bit (aarch64) |
| AudioMoth | USB Microphone firmware v1.3.1, 192kHz sample rate, Medium gain |
| Connection | USB Micro-B to USB-A, switch on DEFAULT |
| Device path | `card 2: Microphone [192kHz AudioMoth USB Microphone], device 0` |
| arecord command | `arecord -d 1 -D plughw:2,0 -f S16_LE -r 192000 -c 1 file.wav` |
