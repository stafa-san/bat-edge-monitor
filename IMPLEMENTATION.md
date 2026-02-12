# Soundscape Monitor — Full Implementation Guide

## Project Overview

A real-time acoustic monitoring system that uses an AudioMoth sensor (192kHz USB microphone) connected to a Raspberry Pi 5 for continuous audio capture, dual-model classification, and cloud-synced visualization.

### Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Raspberry Pi 5                     │
│                                                      │
│  AudioMoth USB Mic (192kHz)                         │
│       │                                              │
│       ├──→ AST Service (general soundscape, 527 cat)│
│       │       → downsamples to 16kHz for inference   │
│       │       → top 5 labels per 1-sec sample        │
│       │       → computes SPL (dB)                    │
│       │                                              │
│       └──→ BatDetect2 Service (bat echolocation)    │
│               → 5-sec segments at 192kHz             │
│               → species-level bat call detection     │
│               → call parameters (freq, duration)     │
│                                                      │
│  PostgreSQL (local) ←── both services write here     │
│       │                                              │
│  Sync Service ──→ Firebase Firestore (cloud)        │
└─────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│  Next.js Dashboard (Vercel)                          │
│  - Real-time Firestore listeners                     │
│  - Sound class distribution charts (Recharts)        │
│  - Bat detection feed with species, freq, duration   │
│  - SPL trend timeline                                │
│  - Stats cards                                       │
└─────────────────────────────────────────────────────┘
```

---

## Project Structure

```
bat-edge-monitor/
├── README.md
├── .gitignore
├── IMPLEMENTATION.md          # This file
│
├── edge/                      # All Pi-side code
│   ├── docker-compose.yml     # Orchestrates 4 services
│   ├── init.sql               # PostgreSQL schema
│   ├── .env                   # FIREBASE_PROJECT_ID
│   ├── .env.example
│   │
│   ├── ast-service/           # General soundscape classification
│   │   ├── Dockerfile         # python:3.11-slim + alsa-utils
│   │   ├── requirements.txt
│   │   └── src/
│   │       ├── __init__.py
│   │       ├── audio_device.py   # AudioMoth capture via arecord
│   │       ├── classifier.py     # AST model inference
│   │       ├── spl.py            # Sound pressure level
│   │       └── main.py           # Capture → classify → store loop
│   │
│   ├── batdetect-service/     # Bat echolocation detection
│   │   ├── Dockerfile         # python:3.10-slim (batdetect2 compat)
│   │   ├── requirements.txt
│   │   └── src/
│   │       ├── __init__.py
│   │       └── main.py        # Capture → bat detect → store loop
│   │
│   └── sync-service/          # PostgreSQL → Firestore sync
│       ├── Dockerfile         # python:3.11-slim
│       ├── requirements.txt
│       ├── serviceAccountKey.json  # Firebase admin key (gitignored)
│       └── src/
│           ├── __init__.py
│           └── main.py        # Batch sync every 60s
│
└── dashboard/                 # Next.js web dashboard
    ├── package.json
    ├── next.config.js
    ├── tsconfig.json
    ├── tailwind.config.js
    ├── postcss.config.js
    ├── .env.local             # Firebase web config (gitignored)
    ├── .env.example
    └── src/
        ├── app/
        │   ├── layout.tsx
        │   ├── globals.css
        │   └── page.tsx       # Main dashboard page
        ├── components/
        │   ├── StatsCards.tsx
        │   ├── SoundscapeChart.tsx
        │   ├── BatDetectionFeed.tsx
        │   └── SPLTimeline.tsx     # TODO: SPL over time chart
        └── lib/
            └── firebase.ts
```

---

## Database Schema (PostgreSQL — edge/init.sql)

```sql
-- General soundscape classifications (AST model)
CREATE TABLE IF NOT EXISTS classifications (
    id SERIAL PRIMARY KEY,
    label VARCHAR(255) NOT NULL,
    score FLOAT NOT NULL,
    spl FLOAT,
    device VARCHAR(100) NOT NULL,
    sync_id UUID NOT NULL,
    sync_time TIMESTAMP NOT NULL,
    synced BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_class_sync_time ON classifications(sync_time);
CREATE INDEX idx_class_device ON classifications(device);
CREATE INDEX idx_class_label ON classifications(label);
CREATE INDEX idx_class_synced ON classifications(synced);

-- Bat echolocation detections (BatDetect2 model)
CREATE TABLE IF NOT EXISTS bat_detections (
    id SERIAL PRIMARY KEY,
    species VARCHAR(255) NOT NULL,
    common_name VARCHAR(255),
    detection_prob FLOAT NOT NULL,
    start_time FLOAT,
    end_time FLOAT,
    low_freq FLOAT,
    high_freq FLOAT,
    duration_ms FLOAT,
    device VARCHAR(100) NOT NULL,
    sync_id UUID NOT NULL,
    detection_time TIMESTAMP NOT NULL,
    synced BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_bat_detection_time ON bat_detections(detection_time);
CREATE INDEX idx_bat_device ON bat_detections(device);
CREATE INDEX idx_bat_species ON bat_detections(species);
CREATE INDEX idx_bat_synced ON bat_detections(synced);
```

---

## Firestore Collections (Cloud)

### `classifications` collection
```json
{
  "label": "Speech",
  "score": 0.045,
  "spl": 33.3,
  "device": "AudioMoth",
  "syncId": "uuid-string",
  "syncTime": "Firestore Timestamp",
  "createdAt": "SERVER_TIMESTAMP"
}
```

### `batDetections` collection
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
  "detectionTime": "Firestore Timestamp",
  "createdAt": "SERVER_TIMESTAMP"
}
```

---

## Edge Services — Detailed Implementation

### 1. AST Service (edge/ast-service/)

**Purpose:** Continuously captures 1-second audio samples at 192kHz, downsamples to 16kHz, classifies using Audio Spectrogram Transformer, computes SPL, stores top 5 labels per sample.

**Key Technical Details:**
- Model: `MIT/ast-finetuned-audioset-10-10-0.4593` from HuggingFace
- Uses sigmoid (not softmax) for multi-label classification — critical for overlapping sounds
- Audio captured via Linux `arecord` command using `plughw` device interface
- Device auto-detected by matching "AudioMoth" in `arecord -l` output
- SPL calculated using scikit-maad with AudioMoth-specific calibration (gain=25, sensitivity=-18, Vadc=1.25)
- Epsilon added to zero-amplitude samples to prevent log10(0) warnings
- Buffers 150 rows (~30 seconds) before flushing to PostgreSQL

**Docker Config:**
- Base: `python:3.11-slim`
- System deps: `alsa-utils`, `libsndfile1`, `libgomp1`
- Requires: `/dev/snd` device passthrough + privileged mode
- Env vars: `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DEVICE_NAME`, `SAMPLE_RATE`

### 2. BatDetect2 Service (edge/batdetect-service/)

**Purpose:** Captures 5-second audio segments at 192kHz, runs BatDetect2 for bat echolocation detection and species classification, stores detections with call parameters.

**Key Technical Details:**
- Model: BatDetect2 (pre-trained on UK bat species)
- Install: `pip install batdetect2`
- Python API: `from batdetect2 import api; results = api.process_file(audio_path)`
- Returns annotations with: class (species), det_prob, start_time, end_time, low_freq, high_freq
- Detection threshold configurable (default 0.3)
- 5-second segments capture full echolocation call sequences
- Logs only when detections found (every 10th segment otherwise to reduce noise)
- Must use Python 3.10 (batdetect2 requires >=3.8, <3.11)

**Docker Config:**
- Base: `python:3.10-slim` (compatibility)
- Same system deps and device access as AST service
- Additional env: `DETECTION_THRESHOLD`, `SEGMENT_DURATION`

### 3. Sync Service (edge/sync-service/)

**Purpose:** Periodically reads unsynced records from local PostgreSQL and batch-writes them to Firebase Firestore.

**Key Technical Details:**
- Uses `firebase-admin` Python SDK
- Requires `serviceAccountKey.json` from Firebase console (mounted read-only)
- Queries `WHERE synced = FALSE`, limits to 500 per batch
- Uses Firestore batch writes for efficiency
- Marks records as `synced = TRUE` after successful write
- Syncs both `classifications` and `bat_detections` tables
- Default interval: 60 seconds

**Docker Config:**
- Base: `python:3.11-slim`
- No audio device access needed
- Requires: `serviceAccountKey.json` volume mount

### 4. PostgreSQL (edge/docker-compose.yml)

- Image: `postgres:16`
- Healthcheck: `pg_isready -U postgres`
- Persistent volume: `pgdata`
- Init script: `init.sql` for schema creation
- Port: 5432 exposed for debugging

---

## Docker Compose (edge/docker-compose.yml)

```yaml
services:
  ast-service:
    build: ./ast-service
    devices:
      - /dev/snd:/dev/snd
    privileged: true
    environment:
      - DB_HOST=db
      - DB_NAME=soundscape
      - DB_USER=postgres
      - DB_PASSWORD=changeme
      - DEVICE_NAME=AudioMoth
      - SAMPLE_RATE=192000
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  batdetect-service:
    build: ./batdetect-service
    devices:
      - /dev/snd:/dev/snd
    privileged: true
    environment:
      - DB_HOST=db
      - DB_NAME=soundscape
      - DB_USER=postgres
      - DB_PASSWORD=changeme
      - DEVICE_NAME=AudioMoth
      - SAMPLE_RATE=192000
      - DETECTION_THRESHOLD=0.3
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  sync-service:
    build: ./sync-service
    environment:
      - DB_HOST=db
      - DB_NAME=soundscape
      - DB_USER=postgres
      - DB_PASSWORD=changeme
      - SYNC_INTERVAL=60
      - FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID}
    volumes:
      - ./sync-service/serviceAccountKey.json:/app/serviceAccountKey.json:ro
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  db:
    image: postgres:16
    environment:
      POSTGRES_DB: soundscape
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: changeme
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  pgdata:
```

---

## Dashboard — Detailed Implementation

### Tech Stack
- Next.js 15 (App Router)
- TypeScript
- Tailwind CSS
- Recharts for data visualization
- Firebase JS SDK for real-time Firestore listeners
- Deployed on Vercel

### Pages

**`/` — Main Dashboard**
- Stats cards: total classifications, bat detections, avg SPL, unique sound classes
- Sound class distribution bar chart (top 10 labels by occurrence)
- Bat detection live feed (species, frequency range, duration, confidence)
- Recent classifications table with score bars
- Real-time updates via Firestore `onSnapshot` listeners

### Future Pages (TODO)
- `/analytics` — Historical trends, hourly/daily aggregations, SPL timeline
- `/devices` — Device status, health monitoring, uptime
- `/bats` — Species breakdown, activity patterns by time of night

### Firebase Client Config (dashboard/src/lib/firebase.ts)
```typescript
import { initializeApp, getApps } from "firebase/app";
import { getFirestore } from "firebase/firestore";

const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
};

const app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];
export const db = getFirestore(app);
```

### Required Firestore Indexes
Create composite indexes in Firebase Console for these queries:
- `classifications`: `syncTime` DESC
- `batDetections`: `detectionTime` DESC

---

## Environment Variables

### Edge (.env in edge/ directory)
```
FIREBASE_PROJECT_ID=your-firebase-project-id
```

### Dashboard (.env.local in dashboard/ directory)
```
NEXT_PUBLIC_FIREBASE_API_KEY=your-api-key
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=your-project-id
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=your-project.appspot.com
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=your-sender-id
NEXT_PUBLIC_FIREBASE_APP_ID=your-app-id
```

---

## Deployment Steps

### Edge (Raspberry Pi 5)
1. Ensure AudioMoth is connected with switch on DEFAULT
2. Place `serviceAccountKey.json` in `edge/sync-service/`
3. Create `edge/.env` with Firebase project ID
4. Run: `cd edge && docker compose up --build`
5. Verify: `docker compose exec db psql -U postgres -d soundscape -c "SELECT COUNT(*) FROM classifications;"`

### Dashboard (Vercel)
1. Push to GitHub
2. Import repo in Vercel
3. Set root directory to `dashboard`
4. Add all `NEXT_PUBLIC_FIREBASE_*` env vars in Vercel settings
5. Deploy

---

## Hardware Setup Summary

**AudioMoth Configuration:**
- Firmware: AudioMoth-USB-Microphone v1.3.1
- Sample Rate: 192 kHz
- Gain: Medium
- Filter: None
- Switch: DEFAULT when recording

**Raspberry Pi 5:**
- OS: Raspberry Pi OS 64-bit (aarch64)
- Docker installed
- AudioMoth connected via USB-A
- Device appears as: `card 2: Microphone [192kHz AudioMoth USB Microphone]`
- Test command: `arecord -d 3 -D plughw:2,0 -f S16_LE -r 192000 -c 1 test.wav`

---

## Known Issues and Considerations

1. **AudioMoth Flash App**: Does not support ARM64/aarch64. Flash firmware from macOS/Windows machine. One-time operation.
2. **BatDetect2 Python version**: Requires Python >=3.8, <3.11. Uses separate Python 3.10 container.
3. **Audio device contention**: Both AST and BatDetect2 services access the same USB mic. The `plughw` interface allows shared access, but simultaneous recording from same device may cause issues. If this happens, implement a shared audio capture service that distributes samples to both classifiers.
4. **SPL calibration**: Current gain/sensitivity values are AudioMoth defaults. Calibrate with a reference sound source for accurate absolute dB readings.
5. **BatDetect2 model**: Pre-trained on UK bat species. For North American bats (Cincinnati area), the model may need fine-tuning. The `train` branch of batdetect2 repo has tools for this.
6. **Firestore costs**: Free tier allows 50K reads/day and 20K writes/day. With 60-second sync intervals and ~500 records per sync, this should stay within limits for a single device.

---

## Research Context

**Thesis:** "Beyond Single Sensors: Quantifying Data Integrity in Multi-modal Edge Systems for Real-Time Ecological Monitoring"

**Key contributions this system enables:**
- Dual-model validation: AST and BatDetect2 provide independent acoustic assessments
- Data integrity metrics: sync status, capture failures, classification confidence distributions
- Edge computing: all inference runs on Pi 5, only results synced to cloud
- Multi-modal: audio classification + SPL measurement + temporal patterns
