# Offline WAV Analysis

Upload a `.wav` from the dashboard, get bat-call detections back in-place — from any network, no LAN access to the Pi required.

## Why

We need Dr. Johnson (and anyone else on an approved link) to be able to validate that the classification pipeline works on known recordings without waiting for a wild bat to fly past the Pi, and without being on the Pi's Wi-Fi. The old `analysis-api` exposed an HTTP port and only worked when the browser and Pi were on the same LAN. Now uploads flow through Firebase, so anywhere with a browser works.

## Pipeline parity with the Pi's live capture path

The upload worker calls the same 4-gate pipeline the Pi's `batdetect-service` runs on every live mic capture. A WAV uploaded from a browser anywhere in the world gets the identical treatment as a WAV the AudioMoth produced:

1. **HPF at 16 kHz** (in-memory) — defensive; bat calls are >20 kHz, everything below is pre-removed.
2. **BatDetect2** at a permissive diagnostic threshold for logging, then a user-threshold gate (`det_prob ≥ 0.5`) matching training.
3. **Groups classifier head** (NA 5-class: EPFU_LANO, LABO, LACI, MYSP, PESU) with `prediction_confidence ≥ 0.6`.
4. **FM-sweep + low-band-ratio shape filter** (per detection) — rejects broadband clicks.
5. **Segment-level audio validator** (RMS / bat-band SNR / burst ratio) — rejects silence and broadband noise.

### Single source of truth

The pipeline lives in [`edge/batdetect-service/src/bat_pipeline.py`](edge/batdetect-service/src/bat_pipeline.py). The cloud worker's `Dockerfile` COPYs that file (and `audio_validator.py`, `classifier.py`) into the analysis-api image at build time. Any pipeline change is one edit to `bat_pipeline.py`; both deployments pick it up on next build.

Every detection written to Postgres and Firestore carries a `pipelineVersion` field (currently `v1-2026-04-22`). If Pi and Cloud drift, detection rows will show mismatched versions and the drift is visible in the DB.

### Dashboard-visible rejection reasons

A live capture that doesn't pass all gates just moves on — no row written. That's correct for continuous capture (hardware won't be confused by silence). But for an *upload*, the user is staring at the dashboard waiting for an answer. So the worker always writes a `rejectionReason` + `rejectionMessage` to the `uploadJobs` doc when `detectionCount=0`:

| Rejection code (prefix) | Human-readable message |
|---|---|
| `batdetect2_no_detections` | "BatDetect2 found no echolocation signatures in this recording." |
| `all_below_user_threshold` | "Detected signals, but none above the confidence threshold." |
| `all_below_min_pred_conf` | "Signals found, but classifier confidence was below the keep-threshold — no bat species identified." |
| `shape:broadband_noise(...)` | "Detected signals, but all looked like broadband clicks rather than bat calls." |
| `shape:chaotic_peaks(...)` | "Detected signals with erratic frequency patterns — not a downward-sweep bat call." |
| `shape:not_downward_sweep(...)` | "Detected signals but none had the downward frequency sweep of a bat call." |
| `validator:rms_too_low(...)` | "Audio appears to be silence or very quiet — no bat calls." |
| `validator:snr_too_low(...)` | "Audio is mostly broadband noise — no bat-call signature detected." |
| `validator:no_burst(...)` | "Audio has no transient burst — likely steady-state noise, not an echolocation pass." |

The dashboard renders `rejectionMessage` verbatim in the upload card when `detectionCount=0`, so the user never sees a blank "0 detections" and wonders if the system is broken.

## Deploy sync strategy (Pi ↔ Cloud)

When you edit `bat_pipeline.py` or any of its dependencies (`audio_validator.py`, `classifier.py`):

| What changed | Pi rebuild | Cloud rebuild |
|---|---|---|
| `bat_pipeline.py` | `docker compose build batdetect-service analysis-api && docker compose up -d` on Pi | `firebase deploy --only functions` from your Mac (Stage 5+, once the Firebase Cloud Function is deployed) |
| `audio_validator.py` / `classifier.py` | same as above | same as above |
| Pi-only code (e.g. ALSA capture in `batdetect-service/src/main.py`) | Pi rebuild | no cloud rebuild needed |
| Cloud-only code (e.g. Firestore trigger in Cloud Function) | no Pi rebuild needed | cloud redeploy |

To detect drift: query for `SELECT DISTINCT pipeline_version FROM bat_detections` in Postgres or inspect recent `batDetections` Firestore docs. More than one version value across the two deployments = drift.

## How it works

```
┌──────────────┐   1. upload .wav      ┌─────────────────┐
│  Dashboard   │──────────────────────▶│ Firebase Storage│
│  (Vercel)    │                       │  uploads/{id}   │
│              │   2. create job       ┌─────────────────┐
│              │──────────────────────▶│ Firestore       │
│              │                       │  uploadJobs/{id}│
│              │   6. subscribe        │   status=pending│
│              │◀──────────────────────│                 │
│              │                       └─────────────────┘
│              │                              │  ▲
│              │                              │  │
│              │                 3. poll      │  │ 5. status+detections
│              │                              ▼  │
│              │                       ┌─────────────────┐
│              │                       │ Pi upload-worker│
│              │                       │ (polls every 5s,│
│              │                       │  downloads WAV, │
│              │                       │  runs BatDetect2│
│              │                       │  + classifier)  │
│              │                       └────────┬────────┘
│              │                                │ 4. insert rows
│              │                                ▼
│              │                       ┌─────────────────┐
│              │                       │  Postgres       │
│              │                       │ bat_detections  │
│              │                       │  source=upload  │
│              │                       │  synced=TRUE    │
│              │                       └─────────────────┘
│              │   7. batDetections                ▲
│              │◀───────────────────────(direct)───┘
│              │   where syncId=jobId
└──────────────┘
```

Two notable quirks:

1. **Worker writes Firestore directly** (step 7) instead of waiting for `sync-service` to pick up the Postgres rows. This gets upload results onto the dashboard immediately instead of on the next 60 s sync cycle. Postgres rows are inserted with `synced=TRUE` so `sync-service` skips them.
2. **WAVs age out automatically.** A 7-day GCS object lifecycle rule deletes everything under `uploads/` — no worker code to clean up, no risk of bucket bloat.

## Data model

### Firestore `uploadJobs/{jobId}`

| Field                 | Who writes           | Notes                                      |
|-----------------------|----------------------|--------------------------------------------|
| `status`              | dashboard → worker   | `pending` → `processing` → `done` / `error` |
| `filename`            | dashboard            | original browser file name                  |
| `sizeBytes`           | dashboard            | ≤ 100 MB (enforced by both rules + worker) |
| `createdAt`           | dashboard            | server timestamp                            |
| `processingStartedAt` | worker               |                                            |
| `completedAt`         | worker               |                                            |
| `durationSeconds`     | worker               | read from WAV header                       |
| `detectionCount`      | worker               |                                            |
| `speciesFound`        | worker               | deduped predicted_class / species list      |
| `errorMessage`        | worker (on failure)  | truncated to 500 chars                      |

### Firestore `batDetections` (existing collection)

Upload-sourced rows get `source='upload'` and `syncId=<jobId>`. The dashboard queries `where source=='upload' && syncId==jobId` to pull per-upload detections. No schema change to the existing collection.

### Firebase Storage `uploads/{jobId}.wav`

- UUIDv4 filename enforced by Storage rules (`uploads/<uuid>.wav`)
- `audio/wav` content type
- Max 100 MB
- Deleted automatically 7 days after creation

## Rules

### `firestore.rules`

Anonymous clients can *create* a new `uploadJobs` doc only if it's well-formed (status=pending, size ≤ 100 MB, valid createdAt, valid filename). Everything else (update, delete, reading other clients' jobs) goes through the Admin SDK, which the rules don't apply to.

### `storage.rules`

Anonymous writes to `uploads/{uuid}.wav` with UUIDv4 filename, `audio/wav` content type, size < 100 MB. No public reads — only the worker's Admin SDK downloads.

### Deploying the rules

From the repo root, after a fresh `firebase login`:

```bash
firebase deploy --only firestore:rules,storage:rules
```

## GCS lifecycle (one-time per Pi / per deployment)

[`firebase/storage-lifecycle.json`](firebase/storage-lifecycle.json) encodes the 7-day delete rule. Apply it once per bucket:

```bash
gsutil lifecycle set firebase/storage-lifecycle.json gs://<bucket-name>
# e.g. gs://bat-edge-monitor.firebasestorage.app
```

Verify with `gsutil lifecycle get gs://<bucket-name>`. Re-run whenever the JSON changes.

## Running on the Pi

The worker lives in the existing `analysis-api` container. To deploy the new version:

```bash
cd ~/bat-edge-monitor/edge
docker compose build analysis-api
docker compose up -d analysis-api
docker compose logs -f analysis-api | head -20
```

Expected startup log:

```
[WORKER] Initializing Firebase...
[WORKER] Firebase connected — bucket=bat-edge-monitor.firebasestorage.app
[WORKER] Postgres connected. Polling every 5s...
```

To sanity-check with a CLI upload (no dashboard required):

```bash
# From a laptop that has `gsutil` auth:
JOB_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
gsutil -h "Content-Type:audio/wav" cp ~/some_bat.wav \
    gs://bat-edge-monitor.firebasestorage.app/uploads/${JOB_ID}.wav

# Create the Firestore job doc with the same ID; easiest path is the Firebase
# console: Firestore → uploadJobs → Add document with id=<JOB_ID>, fields
# status=pending, filename=..., sizeBytes=..., createdAt=server timestamp.
```

Then watch `docker compose logs -f analysis-api` — within ~5 s you should see:

```
[WORKER] Processing job <JOB_ID> (some_bat.wav)
[WORKER] Job <JOB_ID>: 3 detections, 15.0s audio
```

## Failure modes and what happens

| What fails                       | Worker behavior                                                      | Dashboard sees                         |
|----------------------------------|----------------------------------------------------------------------|----------------------------------------|
| Storage download (404 / network) | `FileNotFoundError` → status=`error`, `errorMessage` set              | job flips to error with reason         |
| Corrupt / zero-byte WAV          | `soundfile` raises → status=`error`                                   | job flips to error                     |
| BatDetect2 raises                | caught → status=`error`                                               | job flips to error                     |
| No detections found              | status=`done`, `detectionCount=0`, `speciesFound=[]`                  | "0 bat calls found"                    |
| Worker container crash           | job stays `processing` until the worker restarts (docker `restart: unless-stopped`); no auto-reset on restart | user will need to re-upload           |

"Stuck processing" is a known edge case — acceptable for now since the worker doesn't crash in normal use. If it becomes an issue, add a watchdog that resets `processing` jobs older than N minutes.

## Rollback

If the worker is broken or noisy, revert the Dockerfile CMD to the legacy HTTP app:

```dockerfile
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

Then re-expose port 8080 in `docker-compose.yml`. The old LAN-only flow is still there — `src/main.py` is unchanged.

## Known limitations

- **AST is dead code.** The Acoustic Environment / AudioSet panel was already disabled via the Docker profile on the Pi; the worker does not run AST either. The code in `src/main.py` still compiles if you ever flip back to the HTTP path.
- **No cancellation.** A user who clicks Upload then walks away can't cancel; the worker will still process. Acceptable because processing is typically seconds.
- **No multi-tenant isolation.** Any browser can see any upload's detections (by design — it's a two-person project). Tighten with Firebase Auth later if this becomes multi-user.
- **`speciesFound` uses `predicted_class` when present, else `species`.** For legacy rows with classifier off, you'll see UK species names in the list.
