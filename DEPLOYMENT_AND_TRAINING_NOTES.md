# Bat Edge Monitor — Deployment, Upload, and Training Notes

_Last updated: 2026-03-12_

This document captures the current state of the system, how the two analysis channels work, why the upload panel needs an Analysis API URL, how to replicate the setup on another Raspberry Pi, and how to use your North American bat recordings to improve the bat model.

---

## 1. Current system status

The system is now set up as a **two-channel bat monitoring platform**:

### Channel 1 — Live edge monitoring
- AudioMoth acts as a USB microphone connected to the Raspberry Pi.
- The Pi continuously captures audio and runs:
  - **AST** for general soundscape classification
  - **BatDetect2** for bat-call detection/species classification
- Results are written into local PostgreSQL.
- The sync service pushes those results to Firebase/Firestore.
- The Next.js dashboard reads from Firestore and shows the live feed.

### Channel 2 — Offline WAV upload
- A separate **Analysis API** runs on the Pi on port `8080`.
- From the dashboard, a user can upload an existing `.wav` file.
- The API runs AST and/or BatDetect2 on that file.
- The results are written into the same PostgreSQL database.
- Upload-origin records are tagged with `source='upload'`.
- The sync service pushes them to Firestore like live records.
- The dashboard shows uploaded detections with an `UPLOAD` badge.

### Current readiness
- **Channel 1 (live monitoring):** ready for field use, with the normal caveat that bat species predictions are only as good as the underlying model.
- **Channel 2 (upload analysis):** implemented and working end-to-end.
- **Main limitation:** BatDetect2 is much stronger on UK/European training distributions than on North American species.

---

## 2. Current runtime configuration

### Hardware
- Raspberry Pi 5
- AudioMoth in USB microphone mode
- AudioMoth detected by ALSA as a USB microphone

### Sample rate
The project is currently standardized on:

- `SAMPLE_RATE=250000`

This matches the AudioMoth 250 kHz mode currently being used.

### Active edge services
The `edge/docker-compose.yml` stack currently contains:

1. `ast-service`
2. `batdetect-service`
3. `sync-service`
4. `db` (PostgreSQL)
5. `analysis-api`

### Important ports
- PostgreSQL: `5432`
- Analysis API: `8080`

---

## 3. Data flow

### Live channel

```text
AudioMoth -> AST service / BatDetect2 service -> PostgreSQL -> Sync service -> Firestore -> Dashboard
```

### Upload channel

```text
Browser upload -> Analysis API on Pi -> PostgreSQL -> Sync service -> Firestore -> Dashboard
```

---

## 4. Why the upload panel needs an Analysis API URL

The dashboard is deployed on Vercel, but the upload analysis models do **not** run on Vercel.
They run on the Raspberry Pi inside the `analysis-api` container.

That means the browser needs a direct network address for the Pi.

### Typical values
Use whichever address your browser can reach:

- `http://raspberrypi.local:8080`
- `http://192.168.x.x:8080`
- `http://localhost:8080` (only if the dashboard is being opened on the Pi itself)

### Why this is needed
The upload is sent from the browser directly to the Pi because:
- the models are too heavy to run in the browser
- the models are not hosted on Vercel
- the Pi is the machine that has the local analysis service

### Current dashboard behavior
- The upload panel stores the URL in browser `localStorage`
- It can auto-guess a local URL in some LAN cases
- It includes a **Test Connection** button that calls `GET /health`

### Optional improvement
If you want the field to always be pre-filled in production, set:

```env
NEXT_PUBLIC_ANALYSIS_API_URL=http://raspberrypi.local:8080
```

in the Vercel project settings.

---

## 5. Database/source behavior

Both analysis paths write to the same tables:

- `classifications`
- `bat_detections`

Both tables now include:

- `source VARCHAR(20) DEFAULT 'live'`

This means:
- live microphone records are stored as `source='live'`
- uploaded file analysis records are stored as `source='upload'`

This makes it easy to separate or compare live and uploaded results in the dashboard and in Firestore.

---

## 6. Is the system ready for live deployment?

### Yes, with an important note
The infrastructure side is ready enough to deploy and start collecting:

- live microphone capture works
- local database storage works
- Firebase sync works
- Vercel dashboard works
- offline WAV upload works
- source tagging works
- device health and sample rate reporting are in place

### The main caution
BatDetect2 can still produce false positives, especially because your recordings are from **North America** and the default BatDetect2 model is not specialized for your regional species distribution.

So the system is ready for:
- real deployment
- continuous collection
- iterative improvement

But it should be treated as:
- **operationally useful now**
- **scientifically improvable over time**

### Practical recommendation
For field deployment right now:
- keep collecting
- monitor false positives
- consider increasing the detection threshold if too many low-confidence results appear
- retrain or fine-tune for North American species when your training pipeline is ready

---

## 7. BatDetect2 false positives in your setup

You mentioned many false positives in the roughly `33%` to `65%` confidence range.

That pattern is consistent with:
- a model seeing unfamiliar regional species
- out-of-distribution bat calls
- noisy calls or non-bat ultrasound-like sounds being mapped to the closest known class

In other words: the current model is likely trying its best, but on the wrong species domain.

### Immediate mitigation
A simple near-term fix is to raise the effective detection threshold in the bat pipeline if needed.
The current compose file uses:

```env
DETECTION_THRESHOLD=0.3
```

If field results are too noisy, you can experiment with higher values such as:
- `0.5`
- `0.6`
- `0.7`

This will reduce false positives, but may also reduce recall.

---

## 8. Can your 13 GB of WAV files be used to improve the model?

### Yes
Your recordings are useful because they are already grouped by species in folders.
That is a strong starting point.

Example structure:

```text
dataset/
  Eptesicus_fuscus/
    file001.wav
    file002.wav
  Lasiurus_borealis/
    file101.wav
    file102.wav
```

This gives you **species labels at the file/folder level**.

### Important caveat
BatDetect2 training typically benefits from **call-level annotations** inside each file, such as:
- start time
- end time
- low frequency
- high frequency
- class/species name

Folder labels alone say **what species the file belongs to**, but not necessarily **where inside the file each bat call is located**.

### Practical strategy
A realistic pipeline is:

1. Use the existing detector to locate probable bat calls in each file
2. Assign the parent-folder species as the provisional label
3. Generate training annotations automatically
4. Review a subset manually for quality control
5. Fine-tune the classifier on the cleaned North American dataset

This is the best path if your recordings are already organized by species.

---

## 9. Recommended training plan for your hardware

You said your main machine is a MacBook Pro with M3 Pro, and otherwise you could use Google Colab or rent a GPU.

### Recommended split
Use the MacBook for:
- data prep
- folder scanning
- annotation generation
- metadata cleanup
- train/validation split creation

Use Google Colab or a rented GPU for:
- actual model fine-tuning
- longer training runs
- repeatable experiments

### Why
Training is the expensive part.
Data preparation is comparatively light.

The MacBook is fine for dataset preparation and small tests, but cloud GPU training is the safer long-term path.

### Best option order
1. **Google Colab** — cheapest place to start
2. **Rented NVIDIA GPU server** — best for reliability/faster iteration
3. **MacBook-only training** — possible for experiments, but not the preferred production path

---

## 10. Suggested retraining workflow

```text
Folder-labeled North American WAVs
        ->
Data prep script
  - scan folders
  - infer species from folder names
  - run detector to find candidate calls
  - write training annotations
  - create train/val split
        ->
Colab / cloud GPU training
  - fine-tune BatDetect2 classifier
  - evaluate false positives / recall
        ->
Export trained model
        ->
Deploy model back to Raspberry Pi
```

### Expected benefits
A retrained or fine-tuned model should:
- reduce false positives on North American recordings
- improve species relevance for your deployment region
- make the live detection feed more trustworthy

---

## 11. Second Raspberry Pi setup notes

If you want to replicate this system on another Pi later, this is the minimum checklist.

### Hardware checklist
- Raspberry Pi 5
- AudioMoth with USB microphone firmware
- power supply
- microSD / storage
- network access
- USB cable for AudioMoth

### Software checklist on the Pi
- Docker installed
- Docker Compose available
- repo cloned
- Firebase service account JSON copied into:

```text
edge/sync-service/serviceAccountKey.json
```

- `edge/.env` created with:

```env
FIREBASE_PROJECT_ID=bat-edge-monitor
```

### Bring-up steps

```bash
cd ~/bat-edge-monitor/edge
docker compose up --build -d
```

### Verify the stack

```bash
docker compose ps
arecord -l
docker compose logs --tail 50 ast-service
docker compose logs --tail 50 batdetect-service
docker compose logs --tail 50 sync-service
curl http://localhost:8080/health
```

### What to verify
- AudioMoth appears in `arecord -l`
- all five services are running
- database is healthy
- sync service is writing to Firebase
- dashboard shows new records
- upload panel can reach `http://<pi-hostname-or-ip>:8080`

### Important runtime values to keep aligned
- `SAMPLE_RATE=250000`
- `DETECTION_THRESHOLD=0.3` initially
- the correct Firebase service account key
- the correct Pi hostname/IP for upload access

---

## 12. Current notable implementation details

### Analysis API
- service name: `analysis-api`
- port: `8080`
- supports `POST /analyze`
- supports `GET /health`
- uses CORS so the dashboard can call it from the browser
- lazy-loads AST and BatDetect2 on first use to save Pi memory

### Upload panel
- supports `.wav` upload
- lets you choose whether to run AST and/or BatDetect2
- supports custom `device_label`
- supports `top_k`
- has a connection-test button
- persists the API URL in browser storage

### Sync behavior
Uploaded results go through the same sync pipeline as live results, so the dashboard and Firestore stay unified.

---

## 13. What still remains for future improvement

Recommended next improvements:

1. Add a more opinionated false-positive filter for low-confidence bat detections
2. Build the folder-labeled dataset prep script
3. Build a Colab-ready training notebook/workflow
4. Add model versioning so the dashboard can show which bat model produced each detection
5. Add region-aware deployment notes for North American species
6. Add optional bat-audio upload archiving if you want examples for later review

---

## 14. Bottom line

### Deployment status
Yes — the system is solid enough to deploy now for real-world collection and continued iteration.

### Two-channel status
Yes — it now supports both:
- live AudioMoth monitoring
- offline WAV upload analysis

### Training data status
Yes — your 13 GB of folder-labeled North American WAV files are useful and can form the basis of a retraining pipeline.

### Best next move
The best next move is:
1. keep collecting live data
2. reduce noise with threshold tuning if needed
3. build the dataset-prep pipeline from your folder-labeled recordings
4. fine-tune the bat model in Colab or on a rented GPU
5. redeploy the improved model to the Pi
