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

## Permissive mode (per-upload override)

The upload form has a "Permissive mode" checkbox below the file picker. When ticked, that *one* upload runs against a relaxed threshold preset; the deploy-wide defaults are not touched and other in-flight uploads are unaffected. The checkbox resets after each submit so the relaxed preset can never silently sticky on.

**The five gates lowered:**

| Gate | Default | Permissive | Why this floor catches things in default mode |
|---|---|---|---|
| `DETECTION_THRESHOLD` (BatDetect2 user threshold) | 0.30 | 0.15 | Quiet / distant passes have low det\_prob |
| `MIN_PREDICTION_CONF` (groups classifier) | 0.30 | 0.20 | NA-trained classifier is conservative on partial calls |
| `VALIDATOR_MIN_RMS` | 0.002 | 0.0008 | Faint recordings sit at 0.001–0.0018 RMS |
| `VALIDATOR_MIN_BURST_RATIO` | 3.0× | 1.5× | Amplitude-triggered captures lack a quiet baseline by design |
| `FM_SWEEP_MIN_R2` | 0.20 | 0.10 | Short FM sweeps don't fit a clean linear regression |

**How it's wired:**

* The dashboard form ([UploadAnalysisPanel.tsx](dashboard/src/components/UploadAnalysisPanel.tsx)) writes `permissiveMode: true` on the `uploadJobs/{id}` doc when the checkbox is set.
* The Cloud Function ([functions/main.py](functions/main.py)) reads `permissiveMode` from the job doc and merges `PERMISSIVE_OVERRIDES` into the in-memory pipeline config for that one run.
* The CF echoes `permissiveMode: true` back onto the job doc on completion so the UI can render an amber `permissive` pill on the job card. Default-threshold runs have no `permissiveMode` field at all.

**Why the burst-ratio override matters (2026-04-25):**

The first version of this feature mirrored the Pi's PNM, which lowers four thresholds (detection / classifier conf / RMS / FM-sweep R²). The first A/B test on Dr. Johnson's `20210326_235900T.WAV` showed permissive mode passing the RMS gate (0.0016 > 0.0008) but getting rejected one gate later with `validator:no_burst(1.72x)`. Dr. Johnson's archive is amplitude-triggered (the trigger fires on loud bursts and the WAV starts at the call), so the file lacks the quiet baseline the burst test was designed to use as a comparison point. Lowering the burst floor to 1.5× lets these files through; the live Pi (which captures continuously and *does* have quiet baselines between passes) keeps the 3.0× default.

**Promotion path to the Pi:**

This 5-gate mix lives only in the offline Cloud Function for now. If field experience shows it's the right relaxed preset for archival amplitude-triggered data, mirror `validator_min_burst_ratio=1.5` into the Pi's PNM `.env` block (currently 4 thresholds; would become 5).

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

## Deploy — Firebase Cloud Function (primary)

The offline analysis worker runs as a Firebase Cloud Function (2nd gen, Python 3.12) triggered by Firestore `uploadJobs/{jobId}` doc creation. Event-driven, scales to zero between requests, no Pi dependency.

### One-time: make sure billing is enabled

Cloud Functions 2nd gen requires the Blaze plan. You likely already have it enabled — confirm at https://console.firebase.google.com/project/bat-edge-monitor/usage.

### Deploy from your Mac

```bash
cd ~/source/bat-edge-monitor
firebase deploy --only functions
```

What happens under the hood:

1. `firebase.json`'s `predeploy` hook runs `./functions/build.sh`, syncing `bat_pipeline.py`, `audio_validator.py`, `classifier.py`, and `groups_model.pt` from `edge/batdetect-service/src/` and `docker/models/` into `functions/`.
2. The `functions/` directory is packaged and uploaded to Google Cloud Build.
3. Cloud Build runs the Python buildpack: `pip install -r requirements.txt` pulls torch, batdetect2, firebase-admin, scipy, etc.
4. The resulting container image is deployed to Cloud Run (Cloud Functions 2nd gen is Cloud Run underneath).
5. Eventarc registers the Firestore trigger on `uploadJobs/{jobId}`.

**First deploy takes ~10 min** (Cloud Build compiles a ~1 GB container image with torch). Subsequent deploys are ~2-3 min when only source files changed.

### Verify after deploy

```bash
# List the deployed function
firebase functions:list

# Expected:
#   process_upload (python312, us-central1, firestore trigger: uploadJobs/{jobId})
```

To watch function logs live during testing:

```bash
firebase functions:log --only process_upload
```

### Cold start / warm request latency

- **Cold start** (first request after ~15 min idle): 20-30 s (torch import + model load dominate)
- **Warm request**: 3-5 s for a typical 15 s WAV
- **Timeout**: 540 s (9 min)
- **Memory**: 4 GB, **CPU**: 2 vCPU, **concurrency**: 1 (one WAV per instance at a time)

### Cost at grad-project scale

Function invocation (~50 uploads/month expected): well within Cloud Functions 2nd gen free tier (2M invocations/month, 360k GB-seconds/month). Negligible Storage + Firestore R/W. Expected monthly cost: **$0**.

## Retired: Pi-side polling worker

The Pi previously ran `analysis-api` as a polling worker. That container is now behind the `legacy-upload-worker` Docker Compose profile — **not started by default**. If you ever want to run it alongside the cloud function (for debugging, or as a fallback if the cloud function is down), bring it up with:

```bash
docker compose --profile legacy-upload-worker up -d analysis-api
```

The polling worker and the cloud function race to claim pending jobs. Cloud function is event-triggered, so it usually wins. Both writing to the same `uploadJobs` doc is idempotent in the happy path but not recommended long-term — pick one.

## Running on the Pi (legacy polling path)

If you do re-enable the legacy worker:

```bash
cd ~/bat-edge-monitor/edge
docker compose --profile legacy-upload-worker build analysis-api
docker compose --profile legacy-upload-worker up -d analysis-api
docker compose logs -f analysis-api | head -20
```

Expected startup log:

```
[WORKER] Initializing Firebase...
[WORKER] Firebase connected — bucket=bat-edge-monitor.firebasestorage.app
[WORKER] Pipeline version: v1-2026-04-22
[WORKER] Ready (Firestore + Postgres). Polling every 5s...
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

## Future features (not yet built)

These are parked here rather than in a ticketing system so the next
person to touch this file has context for what we decided NOT to do
and why.

### Real-time audio scrubbing with synchronised spectrogram cursor

SonoBat has a "play real sound" / "play TE sound" interaction: press
play, and a vertical cursor sweeps across the spectrogram in sync with
the audio position. You can click anywhere on the spectrogram to jump
playback to that timestamp, or drag to play a section. Very useful for
bioacoustics review because the *auditory* and *visual* streams
reinforce each other.

**Why we don't have it yet:**
- Our spectrogram is a pre-rendered static PNG served from Firebase
  Storage. No overlay surface to draw a cursor on without browser-side
  re-rendering.
- The time-expanded audio is a separate `<audio>` element with no
  coordinate knowledge of the PNG below it.

**What it would take:**
- Either render the spectrogram client-side (Web Audio API +
  `<canvas>` + FFT). Gives real interactivity but loses the pixel
  fidelity of matplotlib.
- Or keep the PNG, overlay an SVG layer on top whose x-coordinate
  system matches the plot area. Listen to `<audio>` `timeupdate`
  events and translate current time → pixel position. Click handler
  does the inverse.
- Either way, need to solve: the spectrogram PNG has variable margins
  from matplotlib's `tight_layout`, so the "time axis 0" and
  "time axis max" pixel positions aren't known exactly without
  cropping the image to the plot area. Doable — either crop on the
  server (render the plot area only, no axes), or ship the plot-area
  bounding box as metadata on the upload job.

**Effort estimate:** 1-2 weeks for a polished implementation. Not
blocking thesis work; shortlist for post-MS.

### Spectrogram zoom / pan

SonoBat lets you zoom into a specific call and pan across the file.
Ours is fixed at full-file view. Same approach as the cursor feature:
either render client-side, or provide a zoom API that requests a new
PNG at a different time window from the Cloud Function. The
server-side path is simpler (request zoom, get a new PNG) but adds
round-trip latency; client-side is snappier but engineering-heavy.

### Spectrogram multi-view (viridis + sonobat side-by-side)

Currently the user toggles between the two palettes. Having both
visible at once — especially for committee review — would make
cross-palette sanity-checking easier. Implementation is a layout
change only; both PNGs are already generated.

### Per-detection audio snippet playback

Click a detection row → play just that call (with maybe ±50 ms
padding), in time-expanded form. Server generates a short WAV per
detection (10 KB each). Doable; punishing the Storage budget slightly
but under budget at realistic upload volumes.

### Multi-file batch upload

Currently one WAV at a time. Researchers often review dozens of files
in a session. Batch upload: drop 20 WAVs, each becomes its own
`uploadJobs` entry, processed in parallel by the CF (which scales
horizontally for free). Dashboard shows a progress-per-file grid. Low
engineering cost; main question is whether Dr. Johnson reviews one
file at a time or batches.

### Per-session / per-site dashboards

Group uploads by "review session" or "recording site" with aggregate
metrics (species composition, call rate over time, dominant
frequencies). Useful for field reports.

## Known limitations

- **AST is dead code.** The Acoustic Environment / AudioSet panel was already disabled via the Docker profile on the Pi; the worker does not run AST either. The code in `src/main.py` still compiles if you ever flip back to the HTTP path.
- **No cancellation.** A user who clicks Upload then walks away can't cancel; the worker will still process. Acceptable because processing is typically seconds.
- **No multi-tenant isolation.** Any browser can see any upload's detections (by design — it's a two-person project). Tighten with Firebase Auth later if this becomes multi-user.
- **`speciesFound` uses `predicted_class` when present, else `species`.** For legacy rows with classifier off, you'll see UK species names in the list.
