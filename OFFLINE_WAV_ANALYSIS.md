# Offline WAV Analysis

Upload a `.wav` from the dashboard, get bat-call detections back in-place — from any network, no LAN access to the Pi required.

## Why

We need Dr. Johnson (and anyone else on an approved link) to be able to validate that the classification pipeline works on known recordings without waiting for a wild bat to fly past the Pi, and without being on the Pi's Wi-Fi. The old `analysis-api` exposed an HTTP port and only worked when the browser and Pi were on the same LAN. Now uploads flow through Firebase, so anywhere with a browser works.

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
