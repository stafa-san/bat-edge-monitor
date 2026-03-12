# 🍓 Raspberry Pi Setup Guide — Bat Edge Monitor

**Date:** 12 March 2026  
**Purpose:** Replicate the full bat-edge-monitor stack on a new Raspberry Pi  
**Estimated time:** 1–2 hours (plus ~30 min for Docker image builds)

---

## Hardware Requirements

| Component | Spec | Notes |
|-----------|------|-------|
| **Raspberry Pi** | Pi 5 (4 GB+ RAM, 8 GB recommended) | Pi 4 may work but slower model loading |
| **SD Card / SSD** | 64 GB+ (SSD via USB strongly recommended) | Current Pi uses 20 GB of 229 GB |
| **AudioMoth** | Any version with USB mode | Connected via USB, acts as ALSA audio device |
| **USB cable** | Micro-USB (AudioMoth) to USB-A (Pi) | AudioMoth USB data cable |
| **Power supply** | Pi 5: USB-C 27W (5V/5A) official PSU | Underpowering causes throttling |
| **Cooling** | Heatsink + fan (active cooling) | CPU hits 81°C under load — throttles at 85°C |
| **Internet** | Ethernet or Wi-Fi | Needed for Firestore sync; system buffers offline |
| **Case** | Vented or IP65 for outdoor | Needs airflow for cooling |

---

## Step 1: Flash the OS

1. Download **Raspberry Pi Imager** from https://www.raspberrypi.com/software/
2. Flash **Raspberry Pi OS (64-bit, Lite)** — no desktop needed
   - Bookworm or later
   - Architecture: `aarch64`
3. In Imager settings (gear icon), configure:
   - **Hostname:** e.g. `bat-pi-2`
   - **Enable SSH:** yes (use password or SSH key)
   - **Wi-Fi:** enter your SSID + password
   - **Username/password:** your choice
4. Insert SD card / boot SSD and power on

---

## Step 2: Initial Pi Configuration

SSH into the Pi:
```bash
ssh <username>@<pi-hostname>.local
```

Update the system:
```bash
sudo apt update && sudo apt upgrade -y
```

Set the timezone:
```bash
sudo timedatectl set-timezone America/New_York
```

Enable swap (important for model loading — both ML models spike to ~6 GB combined):
```bash
sudo dphys-swapfile swapoff
sudo sed -i 's/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

---

## Step 3: Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

**Log out and back in** for the group to take effect:
```bash
exit
ssh <username>@<pi-hostname>.local
```

Verify:
```bash
docker --version
docker compose version
```

---

## Step 4: Connect the AudioMoth

1. Plug AudioMoth into Pi via USB
2. Set AudioMoth switch to **USB/OFF** (not CUSTOM or DEFAULT)
3. Verify it appears as an ALSA device:

```bash
arecord -l
```

You should see something like:
```
card 2: AudioMoth [...], device 0: USB Audio [USB Audio]
```

The card number may vary — the software auto-detects it by name.

### AudioMoth Configuration

Use the **AudioMoth Configuration App** (not the USB Microphone App) on a computer to set:

| Setting | Value | Why |
|---------|-------|-----|
| **Sample rate** | 256000 Hz (256 kHz) | Captures ultrasonic bat calls (20–120 kHz). Nyquist = 128 kHz |
| **Gain** | Medium or High | Depends on environment noise |

After configuring, switch to **USB/OFF** and connect to the Pi.

---

## Step 5: Clone the Repository

```bash
cd ~
git clone https://github.com/<your-username>/bat-edge-monitor.git
cd bat-edge-monitor
```

> Replace `<your-username>` with your actual GitHub username. If the repo is private, you'll need to authenticate with `gh auth login` or use an SSH key.

---

## Step 6: Set Up Firebase Credentials

The sync-service needs a Firebase Admin SDK service account key to write to Firestore.

### Option A: Same Firebase project (shared dashboard)
Copy the service account key from your existing Pi:
```bash
# On your current Pi or computer
scp ~/bat-edge-monitor/edge/sync-service/serviceAccountKey.json \
    <username>@<new-pi>:~/bat-edge-monitor/edge/sync-service/serviceAccountKey.json
```

### Option B: New Firebase project (separate dashboard)
1. Go to https://console.firebase.google.com
2. Create a new project (or use the existing `bat-edge-monitor` project)
3. Go to **Project Settings → Service Accounts → Generate New Private Key**
4. Save it as `edge/sync-service/serviceAccountKey.json`
5. Enable **Firestore** in the Firebase Console
6. Deploy security rules:
   ```bash
   npm install -g firebase-tools
   firebase login
   firebase deploy --only firestore:rules
   ```

---

## Step 7: Configure Environment Variables

Edit the `docker-compose.yml` to set the device name for this Pi:

```bash
cd ~/bat-edge-monitor/edge
nano docker-compose.yml
```

Change `DEVICE_NAME` to something unique for this Pi:

```yaml
# In each service that has DEVICE_NAME:
- DEVICE_NAME=AudioMoth-Site2    # unique per Pi
```

### Full environment variable reference

| Variable | Default | Used By | Description |
|----------|---------|---------|-------------|
| `DEVICE_NAME` | `AudioMoth` | ast, batdetect | Unique name for this device — stored with every record |
| `SAMPLE_RATE` | `256000` | ast, batdetect, sync | Must match AudioMoth configuration |
| `DETECTION_THRESHOLD` | `0.3` | batdetect | BatDetect2 confidence threshold (0.0–1.0) |
| `SEGMENT_DURATION` | `5` | batdetect | Audio segment length in seconds |
| `SYNC_INTERVAL` | `60` | sync | Seconds between Firestore sync cycles |
| `DB_HOST` | `db` | all | Postgres hostname (leave as `db` for Docker) |
| `DB_NAME` | `soundscape` | all | Postgres database name |
| `DB_USER` | `postgres` | all | Postgres username |
| `DB_PASSWORD` | `changeme` | all | Postgres password — **change this** |
| `FIREBASE_PROJECT_ID` | — | sync | Your Firebase project ID |
| `FIREBASE_STORAGE_BUCKET` | — | sync | e.g. `bat-edge-monitor.firebasestorage.app` |
| `UPLOAD_BAT_AUDIO` | `false` | batdetect, sync | Set `true` to save + upload bat call .wav files |

---

## Step 8: Build and Start

```bash
cd ~/bat-edge-monitor/edge
docker compose up -d --build
```

First build takes **20–40 minutes** on a Pi 5 (downloads ML models, compiles dependencies). Subsequent builds use Docker cache and take seconds.

Watch the logs:
```bash
# All services
docker compose logs -f

# Individual services
docker compose logs -f ast-service
docker compose logs -f batdetect-service
docker compose logs -f sync-service
```

You should see:
```
ast-service-1  | [AST] Model loaded successfully
ast-service-1  | [AST] Monitoring started - device: plughw:2,0, rate: 256000 Hz
batdetect-service-1  | [BAT] Model loaded successfully
batdetect-service-1  | [BAT] Monitoring started - threshold: 0.3, segment: 5s
sync-service-1 | [SYNC] Firebase connected
sync-service-1 | [SYNC] Starting sync loop (interval: 60s)
```

---

## Step 9: Verify Everything

### Check all containers are running:
```bash
docker compose ps
```

All 4 services should show `Up` (db, ast-service, batdetect-service, sync-service).

### Check the database:
```bash
docker compose exec db psql -U postgres -d soundscape -c "
  SELECT 'classifications' AS tbl, COUNT(*) FROM classifications
  UNION ALL
  SELECT 'bat_detections', COUNT(*) FROM bat_detections
  UNION ALL
  SELECT 'device_status', COUNT(*) FROM device_status
  UNION ALL
  SELECT 'capture_errors', COUNT(*) FROM capture_errors;
"
```

### Check Firestore (in Firebase Console):
- Go to https://console.firebase.google.com → Firestore
- You should see documents appearing in `classifications`, `deviceStatus`, and `healthHistory` collections

### Check the dashboard:
- Your Next.js dashboard (if deployed on Vercel) will automatically pick up data from the new device if it's using the same Firebase project
- The `device` field distinguishes which Pi the data came from

---

## Step 10: Enable Auto-Start on Boot

Docker with `restart: unless-stopped` handles service restarts, but Docker itself needs to start on boot:

```bash
sudo systemctl enable docker
```

That's it — on reboot, Docker starts → Postgres starts → healthcheck passes → all three services start automatically.

---

## Post-Setup: Security Hardening

### Change the default Postgres password:
In `docker-compose.yml`, change `changeme` to a real password in **all four places** (db service + ast + batdetect + sync environment sections).

### Bind Postgres to localhost only:
```yaml
# In docker-compose.yml, under the db service:
ports:
  - "127.0.0.1:5432:5432"   # only accessible from the Pi itself
```

Or remove the `ports` section entirely if you don't need external DB access.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `No devices found matching AudioMoth` | Check `arecord -l` — AudioMoth must be in USB/OFF mode and connected |
| ast or batdetect crashes on startup | Check `docker compose logs <service>` — usually a memory issue, ensure swap is enabled |
| sync-service permission denied | Verify `serviceAccountKey.json` exists and is valid |
| High CPU temp (>85°C) | Add active cooling (fan + heatsink), check ventilation |
| DB connection errors after Postgres restart | Already fixed — services auto-reconnect (as of March 12, 2026 update) |
| Services don't start after reboot | Run `sudo systemctl enable docker` |
| Firestore not updating | Check internet: `ping -c 3 google.com` from the Pi |
| Build fails on Pi | Ensure 64-bit OS and sufficient disk space (need ~10 GB free for build) |

---

## Quick Reference: Daily Commands

```bash
# Check status
cd ~/bat-edge-monitor/edge
docker compose ps

# View live logs
docker compose logs -f

# Restart everything
docker compose restart

# Rebuild after code changes
docker compose up -d --build

# Stop everything
docker compose down

# Stop and delete all data (fresh start)
docker compose down -v

# Check disk usage
docker system df
```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────┐
│  Raspberry Pi 5                             │
│                                             │
│  ┌──────────┐  USB   ┌──────────────────┐  │
│  │AudioMoth ├────────┤ ALSA /dev/snd    │  │
│  └──────────┘        └───────┬──────────┘  │
│                              │              │
│              ┌───────────────┼───────────┐  │
│              │  Docker       │           │  │
│              │               │           │  │
│              │  ┌────────────▼────────┐  │  │
│              │  │  audio device lock  │  │  │
│              │  └──┬──────────────┬───┘  │  │
│              │     │              │       │  │
│              │  ┌──▼──────┐  ┌───▼────┐  │  │
│              │  │ AST     │  │BatDet2 │  │  │
│              │  │ (1s)    │  │ (5s)   │  │  │
│              │  └──┬──────┘  └───┬────┘  │  │
│              │     │             │        │  │
│              │  ┌──▼─────────────▼────┐  │  │
│              │  │  PostgreSQL 16      │  │  │
│              │  └──────────┬─────────┘  │  │
│              │             │             │  │
│              │  ┌──────────▼─────────┐  │  │
│              │  │  sync-service      │  │  │
│              │  │  (every 60s)       │  │  │
│              │  └──────────┬─────────┘  │  │
│              └─────────────┼────────────┘  │
│                            │                │
└────────────────────────────┼────────────────┘
                             │ internet
                    ┌────────▼────────┐
                    │  Firebase       │
                    │  (Firestore +   │
                    │   Storage)      │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Next.js        │
                    │  Dashboard      │
                    │  (Vercel)       │
                    └─────────────────┘
```

---

## File Structure Reference

```
bat-edge-monitor/
├── edge/
│   ├── docker-compose.yml          ← orchestrates all services
│   ├── init.sql                    ← DB schema (runs on first boot)
│   ├── ast-service/
│   │   ├── Dockerfile              ← Python 3.11-slim + ALSA
│   │   ├── requirements.txt        ← torch, transformers, librosa, etc.
│   │   └── src/
│   │       ├── main.py             ← capture loop + AST classification
│   │       ├── audio_device.py     ← ALSA device discovery + recording
│   │       ├── classifier.py       ← HuggingFace AST model wrapper
│   │       └── spl.py              ← sound pressure level calculation
│   ├── batdetect-service/
│   │   ├── Dockerfile              ← Python 3.10-slim + ALSA
│   │   ├── requirements.txt        ← batdetect2, librosa, etc.
│   │   └── src/
│   │       └── main.py             ← capture loop + BatDetect2
│   └── sync-service/
│       ├── Dockerfile              ← Python 3.11-slim
│       ├── requirements.txt        ← firebase-admin, psycopg2
│       ├── serviceAccountKey.json  ← Firebase credentials (DO NOT COMMIT)
│       └── src/
│           ├── main.py             ← sync loop + retention + migrations
│           └── health.py           ← Pi health metrics collector
├── dashboard/                      ← Next.js frontend (deployed to Vercel)
├── firestore.rules                 ← Firestore security rules
├── firebase.json                   ← Firebase CLI config
├── DEPLOYMENT_READINESS.md         ← System audit report
└── SPECIES_CLASSIFICATION.md       ← BatDetect2 species guide for Ohio
```
