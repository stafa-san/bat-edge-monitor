# Session Notes — 2026-04-23

Mac-side session picking up after the Pi-side April 20 sprint. Full end-to-end offline-WAV-analysis workflow shipped: Firebase Cloud Function processing, Dr. Johnson-facing review UI, server-side spectrograms, time-expanded audio, reviewer-scored verify/reject flow, and a real-world threshold tuning based on a false-negative the user caught against the live pipeline.

If you're picking this up from Pi-side VSCode, the key thing to know is: **the cloud is now the primary path for offline WAV analysis**, the Pi's `analysis-api` container is retired behind a `legacy-upload-worker` profile, and both pipelines run identical 4-gate analysis code (`bat_pipeline.py` in `edge/batdetect-service/src/`).

---

## Context — what changed since the Pi's last `git pull`

The Pi was on `origin/dev` up to commit `4f3395c` (`hardening(audio_validator)`) as of April 20. Everything below happened on the Mac between April 22-23 on a branch called `offline-wav-analysis`, merged into `dev` and `main` after each logical chunk. Feel free to `git pull` on `main` and read the commit history — this doc just summarizes the narrative.

### New top-level directories + docs

| Path | What it is |
|---|---|
| `functions/` | Firebase Cloud Function 2nd gen (Python 3.12) that processes uploaded WAVs. `build.sh` predeploy hook syncs shared modules from `edge/batdetect-service/src/` and `docker/models/` into `functions/src/` and `functions/models/`. `.gitignore` keeps the synced copies out of the repo — canonical source stays in `edge/batdetect-service/src/`. |
| `storage.rules` | Firebase Storage rules. Anonymous write to `uploads/<uuid>.wav` (size-capped, audio/wav content-type only). Public read on `spectrograms/*` and `audio/*`. |
| `firebase/storage-lifecycle.json` | GCS lifecycle rule — deletes `uploads/*` after 7 days. One-time `gsutil lifecycle set`. |
| `.firebaserc` | Pins default project to `bat-edge-monitor`. |
| `OFFLINE_WAV_ANALYSIS.md` | Architecture overview for the upload flow. |
| `RETRAINED_NA_DETECTOR_PLAN.md` | Research plan for future detector retraining (NEW today). |
| `DETECTION_TUNING_PLAYBOOK.md` | Already existed from April 20; now references the 0.3 tuning rationale. |

### New edge/shared code

| Path | What it is |
|---|---|
| `edge/batdetect-service/src/bat_pipeline.py` | **Single source of truth for the 4-gate analysis.** Pi's `batdetect-service/main.py` has a nearly-identical inline copy (not yet migrated to import from here — that's deliberate, we agreed to let cloud iterate first); cloud Cloud Function imports from this file via `build.sh`. |
| `edge/batdetect-service/src/spectrogram.py` | Renders labelled PNG spectrograms for uploaded WAVs. `with_boxes=True` → red detection bounding boxes + species labels. `with_boxes=False` → clean spec. matplotlib import is lazy so the Pi can import this module without having matplotlib installed. |

### Dashboard additions

| Path | What it is |
|---|---|
| `dashboard/src/components/UploadAnalysisPanel.tsx` | Full rewrite. Upload progress bar, recent-25-uploads list with click-to-expand, rich per-job view (spectrogram with toggle, metrics cards, confidence histogram, call-density timeline, time-expanded audio player, detection rows with verify/reject/notes, CSV export, reviewer name persistence). |
| `dashboard/src/components/BatDetectionRow.tsx` | Extracted from `BatDetectionFeed.tsx` so both surfaces share one row style. Accepts optional `onReview` callback that renders ✓ / ✗ / 📝 controls. |
| `dashboard/src/lib/firebaseStorage.ts` | Thin wrapper around Firebase Storage's resumable upload with progress callback. |

---

## Pipeline changes by commit (chronological)

All on the `offline-wav-analysis` branch, then merged to `dev` and `main`.

### Base (pre-branch, from April 20 work on dev)

| Commit | What |
|---|---|
| `4f3395c` | `hardening(audio_validator)` — reject invalid bounds + non-finite audio |
| `217c28e` | `fix(audio_validator)` — two false-negative bugs in FM-sweep shape filter |
| `e5b1c24` | `feat(batdetect)` — 4th gate: per-detection FM-sweep + low-band-ratio filter |
| `f3119de` | validator rejection tracking + BD stats persistence + nightly summary email |
| `0b25412` | sub-threshold BD logging + audiomoth config probe |
| `6231b70` | audio-level validator + match-training DETECTION_THRESHOLD |

That's where the Pi was running. All of this is in the live `batdetect-service` container.

### The offline-WAV-analysis branch (April 22-23)

| # | Commit | What |
|---|---|---|
| 4bbe732 | `feat(worker): convert analysis-api to Firebase-driven upload worker` | Initial Firebase-transport upload pipeline. User uploads WAV to Firebase Storage, dashboard creates Firestore `uploadJobs/{id}` doc, Pi worker polls + processes. |
| e4da000 | `refactor(dashboard): extract BatDetectionRow for reuse` | Row component extracted so upload panel shares styling with live feed. |
| 361105e | `feat(dashboard): rewrite UploadAnalysisPanel` | Drops URL/label/topK/AST toggles. Firebase Storage upload with progress bar, Firestore doc subscription, recent-25-uploads list. |
| d22284f | `chore(firebase): pin default project id` | Adds `.firebaserc`. |
| 19f8489 | `feat(pipeline): extract shared 4-gate pipeline` | `bat_pipeline.py` as single source of truth. Pi's inline path stays (by agreement) as the "ground truth" while cloud iterates. |
| ed94248 | `feat(cloud): Firebase Cloud Function` | `process_upload(us-central1)` — Firestore trigger, lazy imports torch/batdetect2 inside the handler body so Firebase CLI parse doesn't need heavy deps. Function runs the same `run_full_pipeline`. Pi `analysis-api` retired behind `legacy-upload-worker` profile. |
| 5a0be7e | `fix(cf): defer heavy imports in main.py` | Move torch/scipy/batdetect2 imports inside the function so `functions/venv` only needs `firebase-functions + firebase-admin`. |
| b1a99be | `fix(dashboard): split live/upload feeds` | Relabel main feed to "Live Bat Detection Feed". Main feed filters `source !== 'upload'`, upload panel filters `source === 'upload'`. Single shared Firestore subscription from `page.tsx` with `limit(500)` instead of 50. |
| be99df8 | `feat(dashboard): Clear all button` | Clear-all for upload history + relaxed Firestore delete rules for upload-sourced rows. |
| 11819e0 | `feat(cloud): server-side spectrogram` | matplotlib-rendered PNG with red detection boxes. Cloud Function uploads to `spectrograms/{jobId}.png`, dashboard displays inline. |
| ad63139 | `feat(dashboard+cloud): ecologist enrichments` | Big one. Metrics cards, confidence histogram, call-density timeline, 10× time-expanded audio WAV, CSV export, verify/reject review UI with reviewer-name persistence, Firestore rules carve-out for review-field updates. |
| a7ff601 | `fix(ui): unclutter spectrogram` | Drop text labels, sort detections by startTime so leftmost box = topmost row. |
| 033cedd | `feat(pipeline+ui): tune threshold to 0.3 + toggleable spectrogram overlay` | First attempt at lowering the threshold (had a subtle bug — see next commit). Spectrogram now renders both clean and annotated variants, dashboard toggle button. |
| **current** | `fix(pipeline): threshold 0.3 end-to-end, restore labels on annotated spec, live-only species chips, NA detector plan, session notes` | Today's batch. Fix the max() bug so 0.3 actually applies, restore species labels on the annotated variant, StatsCards filters out upload-sourced rows, plus the two new docs. |

---

## Why threshold 0.3 matters (the 005517 story)

User uploaded `2MU01134_20240527_005517.wav`, a real Ohio AudioMoth recording. The spectrogram clearly showed ~60 rhythmic FM pulses — obvious bat pass. The pipeline said: **zero detections** (reason: `batdetect2_no_detections`).

That's UK-trained BatDetect2 being systematically under-confident on NA bats, which we already knew about (see `BATDETECT2_TRAINING.md` §"Known caveats"). The documented `det_prob > 0.5` training-distribution threshold was filtering out almost every NA bat candidate before the classifier head saw it.

Lowering the threshold to 0.3 recovered **58 detections** on that file. Validation:
- Known-bad non-bat file → correctly rejected (downstream gates did their job).
- Training-corpus real-bat files → correctly identified.
- Spectrogram visually confirms all 58 are in the rhythmic-pulse region.

The rationale in `bat_pipeline.py`:

> Previous versions force-floored this at CLASSIFIER_TRAINING_DET_THRESHOLD (0.5) via max(). Lowered 2026-04-23: UK-trained BatDetect2 is under-confident on NA bats, and 0.5 was dropping real passes. Downstream gates — classifier min_pred_conf 0.6 + FM-sweep shape filter + audio-level validator — absorb the extra out-of-distribution noise. So we trust user_threshold as-is.

**There was a bug** in the first attempt (commit 033cedd): I lowered `CLASSIFIER_DET_THRESHOLD` constant from 0.5 to 0.3, but left the `threshold = max(user_threshold, CLASSIFIER_DET_THRESHOLD)` logic. Since `user_threshold` defaulted to 0.5, the effective threshold stayed at 0.5 — my "fix" was a no-op. Today's commit actually lands the change: removes the `max()` so `threshold = user_threshold`, and sets `user_threshold` default to 0.3 in all three call sites (cloud function, upload worker, Pi's inline pipeline). `CLASSIFIER_DET_THRESHOLD` was renamed to `CLASSIFIER_TRAINING_DET_THRESHOLD = 0.5` as a documented reference value (no longer enforced as a floor).

### Pi-side impact

`edge/docker-compose.yml` changed `DETECTION_THRESHOLD` from `0.5` to `0.3` on `batdetect-service`. When the Pi next runs `docker compose build batdetect-service && docker compose up -d batdetect-service`, live capture picks up the same tuning. Downstream gates unchanged, so false-positive rate on live captures should stay low.

**To deploy on Pi:**
```bash
cd ~/bat-edge-monitor
git pull
cd edge
docker compose build batdetect-service
docker compose up -d batdetect-service
docker compose logs -f batdetect-service | head -20
# look for "user_threshold=0.3" or just watch for more detections to start rolling in
```

---

## Architecture (post-today)

```
                                           ┌──────────────────────────┐
                                           │  Dashboard (Vercel)      │
                                           │   /main on bat-edge-*    │
                                           └────────┬─────────┬───────┘
                                                    │         │
                       upload .wav + Firestore doc  │         │ subscribe Firestore
                       (any network, any browser)   │         │  batDetections + uploadJobs
                                                    │         │  + deviceStatus + environmentalReadings
                                                    ▼         │
                                           ┌──────────────────┐
                                           │ Firebase Storage │
                                           │ Firestore        │
                                           └────────┬─────────┘
                                                    │
                     Firestore trigger              │
                     on uploadJobs/{id} create      │
                                                    ▼
                                         ┌──────────────────────┐
                                         │ Cloud Function       │
                                         │  process_upload      │
                                         │  us-central1         │
                                         │  python312 · 4 GB    │
                                         │                      │
                                         │  • load WAV          │
                                         │  • bat_pipeline       │
                                         │    ├─ HPF @ 16 kHz  │
                                         │    ├─ BatDetect2     │
                                         │    │  (threshold 0.3)│
                                         │    ├─ Groups classifier│
                                         │    │  head (0.6 conf) │
                                         │    ├─ FM-sweep filter │
                                         │    └─ audio validator │
                                         │  • render 2 spectrograms│
                                         │  • render 10× audio  │
                                         │  • write detections   │
                                         │    back to Firestore  │
                                         └──────────────────────┘

                                         ┌──────────────────────┐
                                         │  Raspberry Pi 5      │
                                         │  batdetect-service   │
                                         │  (live capture)      │
                                         │                      │
                                         │  • arecord 15 s      │
                                         │    segments @ 256 kHz│
                                         │  • SAME 4-gate path  │
                                         │    (inline copy —    │
                                         │    not yet migrated  │
                                         │    to shared module) │
                                         │  • write to Postgres │
                                         └──────────┬───────────┘
                                                    │ sync-service
                                                    ▼
                                         ┌──────────────────────┐
                                         │ Firestore batDetections│
                                         │  source='live'        │
                                         └──────────────────────┘
```

---

## Cloud deploy state

- **Firebase Cloud Function**: `process_upload(us-central1)` — live, python312 runtime, 4 GB memory, 2 vCPU, 9 min timeout. Firestore trigger on `uploadJobs/{jobId}`.
- **Firebase Storage rules**: deployed. Anonymous write to `uploads/`, public read on `spectrograms/` and `audio/`.
- **Firestore rules**: deployed. Anonymous write to `uploadJobs` (with shape validation), delete allowed on `uploadJobs` + on `batDetections` where `source == 'upload'`. Update allowed on `batDetections` only for the review fields (`reviewedBy`, `reviewedAt`, `verifiedClass`, `reviewerNotes`) on upload-sourced rows. Live rows stay fully immutable from clients.
- **GCS lifecycle**: 7-day delete on `uploads/*`.
- **Vercel**: auto-deploys `main` to production.

---

## Pending deploy on Pi

When you next ssh into the Pi and pull:

```bash
cd ~/bat-edge-monitor
git fetch origin
git pull origin main

# rebuild the batdetect-service with the 0.3 threshold (inline change)
cd edge
docker compose build batdetect-service
docker compose up -d batdetect-service

# verify threshold dropped:
docker compose logs batdetect-service | grep "batdetect_threshold"
#   should say 0.3 not 0.5

# analysis-api is now behind a profile — it no longer auto-starts.
# The cloud function replaced it. If you want the legacy polling
# worker for any reason:
docker compose --profile legacy-upload-worker up -d analysis-api
```

No schema migrations. No new Pi-side dependencies (analysis-api adds matplotlib via its Dockerfile, but that container is now profile-gated so a normal `docker compose up -d` skips it). Pi disk impact: zero change.

---

## Deployment drift checklist (for next time)

Things to verify after every big change:

1. **Pi and cloud pipeline versions match.** Both should stamp `pipelineVersion: v1-2026-04-22` (or whatever version is current) on detection rows. Diff via Firestore query:
   ```
   SELECT DISTINCT pipelineVersion, source FROM batDetections
   ```
   If live rows carry a different version from upload rows, drift.

2. **Rules deployed.** After editing `firestore.rules` or `storage.rules`:
   ```
   firebase deploy --only firestore:rules,storage
   ```

3. **Cloud Function redeployed when `bat_pipeline.py` or shared modules change.** `./functions/build.sh` then `firebase deploy --only functions` (10 min first time, 2-3 min incremental).

4. **Pi rebuilt when `batdetect-service/src/` changes.** `docker compose build batdetect-service && docker compose up -d`.

5. **Dashboard auto-deploys via Vercel.** `main` branch push → Vercel builds and deploys the dashboard. Check https://vercel.com/ dashboard for the deploy status.

---

## Known limitations / followups

- Pi's `batdetect-service/main.py` still has an inline copy of the pipeline instead of importing `bat_pipeline.py`. We deliberately left it there so the cloud could iterate without breaking live captures. Now that the threshold tuning is stable across both, migrating the Pi to import from `bat_pipeline` is low-risk — can be done next session.

- Retrained NA detector is scoped but not started. See `RETRAINED_NA_DETECTOR_PLAN.md`. Biggest blocker is bounding-box annotation effort on Dr. Johnson's data.

- Reviewer identity is self-assigned on the dashboard (just a localStorage name) — not authenticated. Fine for current trusted-committee context; tighten if the URL ever goes public.

- Upload-WAV files live in Firebase Storage for 7 days (GCS lifecycle). After that they vanish; only the Firestore detection rows persist. If a reviewer comes back on day 8 to verify a detection, they won't be able to listen to the original WAV. Acceptable trade-off to keep Storage costs bounded.

- The Pi was OFFLINE during the April 21-22 portion of the Mac work — you can see this in `deviceStatus` as a gap. Expected; not a bug.

---

## One-line summary

Offline WAV analysis pipeline is production-ready; retrained NA detector is the next major research track, scoped in `RETRAINED_NA_DETECTOR_PLAN.md`.
