# Session Notes — 2026-04-20

Deployment-day tuning and production-stability fixes after the first
batch of field data showed broadband noise contaminating detections.
Also the first day of actual cloud-sync outage (Firestore quota).

Covers:

1. AudioMoth hardware reconfigure (physical, done by advisor)
2. Pi-side config alignment for the new AudioMoth
3. Confidence-gated tier storage (replacing "save everything")
4. 16 kHz software HPF as a secondary cut
5. AST service disabled behind a compose profile
6. Firestore quota incident + Blaze upgrade + batch-size reduction
7. Dashboard total-count fix for bat detection stats card
8. Outstanding physical fix (USB extension cable)

Related PRs: **#33**, **#34**, **#35**, and the follow-up dashboard PR
opened at the end of this session.

---

## 1. AudioMoth reconfigure — done by Dr. Johnson

Before today the AudioMoth was running in **DEFAULT** switch position,
which bypasses the on-device filter. That let sub-16 kHz energy (fan
noise, wind, rustle) dominate the captured WAVs and flow straight into
BatDetect2 and the classifier head, producing confidently labelled
"bat calls" that were actually machine noise.

New AudioMoth USB Microphone App settings:

| Setting | Value |
| --- | --- |
| Sample rate | 384 kHz |
| Gain | Medium |
| Filter type | High-pass |
| Filter cutoff | 8 kHz |
| Switch position | **CUSTOM** (DEFAULT ignores the filter) |

LED flashes red on the Pi once plugged back in. That's expected.

## 2. Pi-side config for the new AudioMoth

### Sample rate — stays at 256 kHz

The AudioMoth now reports 384 kHz native. BatDetect2's
`target_samp_rate` is 256 kHz. We chose config **(a)**:

- `SAMPLE_RATE=256000` across `batdetect-service`, `analysis-api`,
  and `sync-service`
- Pi asks ALSA for 256 kHz; `plughw` resamples 384 → 256 kHz once
- BatDetect2 then sees exactly its target rate — no internal resample

Setting `SAMPLE_RATE=384000` would force BatDetect2 to do its own
resample on top. No benefit for bats (all NA species <120 kHz), only
extra CPU and archive size.

Comment added to `edge/docker-compose.yml` next to `SAMPLE_RATE` so
future-us doesn't flip it by reflex.

### Detection threshold — 0.3 → 0.5

`DETECTION_THRESHOLD=0.5` in `edge/docker-compose.yml`. With fan
noise no longer dominating the input (thanks to the hardware HPF),
BatDetect2's `det_prob` is a more meaningful signal. The 0.1–0.3 era
let broadband noise through as 30–40 % "detections"; 0.5 keeps those
out.

`CLASSIFIER_DET_THRESHOLD` stays at **0.3** (env var in
`edge/batdetect-service/src/main.py`). It's the secondary inside-the-
segment gate between BatDetect2 and the classifier head; raising the
primary threshold first is always the cleaner knob to turn.

## 3. Confidence-gated tier storage

`edge/batdetect-service/src/storage.py` — AND-gated on **both**
BatDetect2 `det_prob` and classifier `prediction_confidence`. Before
today it was "save everything"
(`TIER1_CONFIDENCE_MIN = 0.0`), which filled Google Drive with noise.

```
THRESHOLDS_LAST_TUNED = "2026-04-20"

TIER1_CONFIDENCE_MIN = 0.7    # classifier prediction_confidence
TIER1_DET_PROB_MIN   = 0.5    # BatDetect2 det_prob
TIER2_CONFIDENCE_MIN = 0.4
TIER2_DET_PROB_MIN   = 0.5
TIER4_CONFIDENCE_MAX = 0.0    # tier 4 never writes
```

| Tier | Condition | What happens |
| --- | --- | --- |
| 1 | `pred_conf ≥ 0.7` AND `det_prob ≥ 0.5` | WAV archived permanently → `/bat_audio/tier1_permanent/<CLASS>/` → Google Drive via rclone |
| 2 | `pred_conf ≥ 0.4` AND `det_prob ≥ 0.5` | WAV archived 30-day → `/bat_audio/tier2_30day/` |
| 3 | anything else that passed `DETECTION_THRESHOLD` | metadata-only row in Postgres, no WAV |
| 4 | — | never writes |

All 5 NA groups (`EPFU_LANO, LABO, LACI, MYSP, PESU`) are tier-1
eligible — we don't artificially restrict rare-species status during
early deployment.

`determine_tier()` now takes `(detection, prediction)` tuples so both
scores are visible. `pick_class_folder()` updated to match.

### Per-detection log line

`edge/batdetect-service/src/main.py` prints this for every detection:

```
-> tier 1/LACI (max det_prob=0.712, max pred_conf=0.814) -> /bat_audio/tier1_permanent/LACI/pi01_....wav
```

Use this to tune thresholds later without re-querying Postgres.

## 4. 16 kHz software HPF — secondary cut

Belt-and-suspenders. The AudioMoth's 8 kHz hardware HPF does the heavy
lifting; the software HPF at 16 kHz makes sure BatDetect2 only sees
bat-relevant band energy regardless of hardware state.

Implementation: scipy `butter` + `sosfiltfilt` (zero-phase), designed
at BatDetect2's target rate (256 kHz). Applied in-memory **between**
`bat_api.load_audio()` and `bat_api.process_audio()`.

Important: the archived WAV written to `/bat_audio/` is the
**unfiltered** one — advisors can inspect it with full context.

Env vars:

- `HPF_ENABLED` (default `true`)
- `HPF_CUTOFF_HZ` (default `16000`)
- `HPF_ORDER` (default `4`)

Startup line to grep for: `HPF enabled: cutoff=16000 Hz, order=4`.

`scipy` added to `edge/batdetect-service/requirements.txt`.

## 5. AST service disabled (profile-gated)

AST was producing ~5 AudioSet labels per 1-second audio sample —
thousands of Firestore writes per day. The dashboard's "Acoustic
Environment" section that consumed those is now collapsed and
de-emphasized. We no longer need AST running for the bat-focused
workflow.

`edge/docker-compose.yml`:

```yaml
ast-service:
  profiles: ["ast"]
  # ...
```

The profile gate means `docker compose up -d` without `--profile ast`
**won't** start it. Reboots don't bring it back. Re-enable anytime:

```bash
docker compose --profile ast up -d ast-service
```

No code changes to `ast-service` itself.

## 6. Firestore quota incident → Blaze + batch-size reduction

### What happened

Free-tier (Spark) Firestore daily write cap is 20K. By mid-afternoon
the quota was exhausted, sync-service started hitting `429 Quota
exceeded` on every push, and the dashboard saw no fresh `lastSeen`
timestamp so it marked the Pi **Offline** even though the Pi was
healthy and still capturing bats into local Postgres.

The main culprit was AST (~5 writes per sample) before it was
disabled, plus healthHistory snapshots every 15s (~11 500/day).

### What's fixed

- **Blaze plan** — pay-as-you-go. Upgraded at the Firebase console.
  Removes the 20K/day hard cap. Still trivially cheap at our volume
  now that AST is off (probably $1–3/month). Reversible: downgrading
  back to Spark just re-enforces the cap.
- **Local backlog cleared** — marked 1,365 unsynced AST classifications
  as synced (we weren't going to upload them anyway) and pruned 37,005
  stale rows older than 1 h to reclaim disk.
- **Batch `LIMIT 500 → 25`** across `sync_classifications`,
  `sync_bat_detections`, `sync_environmental_readings`. After the
  earlier 429 flood, Firestore kept the project in a post-abuse
  ramp-up throttle ("500/50/5 rule"); even 50-doc batches were still
  failing. 25 clears cleanly and is plenty for steady-state traffic
  (usually <10 docs/cycle now).

### Daily volume after the fix

Ball-park, with AST disabled and 15s health interval:

| Source | ~writes/day |
| --- | --- |
| `deviceStatus` overwrite @ 15s | 5 760 |
| `healthHistory` add @ 15s | 5 760 |
| Environmental readings (2 sensors @ 30s) | ~5 700 |
| Bat detections | <500 |
| **Total** | **~17 700** |

That's cutting it close under Spark but fine on Blaze. If we want to
downgrade back to Spark we should also:

- Raise `HEALTH_INTERVAL` to 60s (→ 1 440/day each for deviceStatus
  and healthHistory)
- Decouple `healthHistory` from the 15s tick so it writes only once
  per minute regardless of interval

Those are noted as follow-up work — not needed today.

## 7. Dashboard — total bat detection count

The `StatsCards` "Bat Detections" value was showing `batDetections.length`,
which is capped at 50 by the Firestore `limit(50)` on the live-feed
query. So the summary misled readers into thinking "we've only ever had
50 detections" when local Postgres had 91+.

Fix: read the authoritative count from `deviceStatus.batDetectionsTotal`
(sync-service writes this on every health tick). Fall back to
`batDetections.length` when the field is missing so older docs still
render.

Also updated the feed header from "50 detections" to "last 50 of
{total}" when the total is larger. Each stats card now carries a small
sub-label clarifying whether it's a totals or recent-feed number.

## 8. Outstanding physical fix — USB extension cable

Still TODO from the advisor: run a 1–2 m USB extension so the
AudioMoth is physically away from the Pi's fan. The 8 kHz hardware HPF
+ 16 kHz software HPF keep noise out of the analysis path, but the
archived tier-1 WAVs will still carry fan harmonics until the mic is
moved. Do this next time you're on-site.

---

## Quick reference

**Environment variables that now matter** (all in
`edge/docker-compose.yml` under `batdetect-service`):

| Var | Default | Notes |
| --- | --- | --- |
| `SAMPLE_RATE` | `256000` | Keep at BatDetect2 target rate |
| `DETECTION_THRESHOLD` | `0.5` | BatDetect2 base threshold |
| `CLASSIFIER_DET_THRESHOLD` | `0.3` | Secondary inside-segment gate |
| `SEGMENT_DURATION` | `15` | Matches advisor's training chunks |
| `HPF_ENABLED` | `true` | 16 kHz software HPF pre-BatDetect2 |
| `HPF_CUTOFF_HZ` | `16000` | |
| `HPF_ORDER` | `4` | |

Tier thresholds live in
`edge/batdetect-service/src/storage.py`. Bump
`THRESHOLDS_LAST_TUNED` when you change them.

**Verification commands**

```bash
# batdetect healthy
docker logs edge-batdetect-service-1 --tail 30 | grep -E \
  "Initializing audio capture|HPF enabled|Classifier ready|Monitoring started"

# sync pushing successfully
docker logs edge-sync-service-1 --tail 30 | grep -E "Cycle|Error"

# backlog
docker exec edge-db-1 psql -U postgres -d soundscape -c "
SELECT
  (SELECT count(*) FROM classifications  WHERE synced=FALSE) AS unsynced_cls,
  (SELECT count(*) FROM bat_detections   WHERE synced=FALSE) AS unsynced_bat,
  (SELECT count(*) FROM environmental_readings WHERE synced=FALSE) AS unsynced_env;"

# AudioMoth hardware
lsusb | grep -i audiomoth
cat /proc/asound/card2/stream0 2>/dev/null | grep Rates

# tier folder structure on gdrive
rclone tree "gdrive:Bat Recordings from pi01"
```

**Re-enabling AST if you ever want it back:**

```bash
docker compose --profile ast up -d ast-service
```

---

## Follow-up (same-day, late evening 2026-04-20 → 2026-04-21)

### Single-gate detection model

Post-deployment review showed the tiered model was confusing advisors:
the dashboard feed displayed all bat rows (including tier 3 metadata
only) while only tier 1 was archived and uploaded to Google Drive.
Three different definitions of "real detection" across three surfaces.

**The rule now**: one gate, one story.

| Check | Threshold | Source |
| --- | --- | --- |
| BatDetect2 `det_prob` | `≥ 0.3` | `DETECTION_THRESHOLD` (compose env) |
| Classifier `prediction_confidence` | `≥ 0.6` | `MIN_PREDICTION_CONF` (compose env) |

If a detection clears **both**, it:

1. gets a row in Postgres
2. syncs to Firestore → visible in the dashboard feed
3. has its WAV archived to `/bat_audio/tier1_permanent/<CLASS>/`
4. uploads to Google Drive on the next sync cycle

If it fails either gate, it is dropped silently — no row, no WAV, no
dashboard card. Every downstream consumer sees the same set.

### Bug fix along the way

`_run_batdetect_with_classifier()` was calling `bat_api.process_audio(audio)`
**without** the config dict, so `DETECTION_THRESHOLD=0.5` from earlier
in the day was never actually applied — BatDetect2 ran at its built-in
default of 0.01 and the only thing holding low-prob noise out of the DB
was the secondary `CLASSIFIER_DET_THRESHOLD=0.3` filter. Fixed: the
config is now passed, so the primary threshold works.

### Tier thresholds are dead code

`TIER1_*`, `TIER2_*`, `TIER4_*` constants in
`edge/batdetect-service/src/storage.py` are kept at zero/unused
because filtering now happens upstream in `main.py` before
`determine_tier()` ever sees a row. `determine_tier()` always returns
`1` on any non-empty input. Kept the scaffolding so we can reintroduce
a review-only tier later without restructuring.

### Threshold calibration

`DETECTION_THRESHOLD` dropped from 0.5 → **0.3**. BatDetect2 UK-trained
genuinely cannot score NA bats above ~0.4 `det_prob`, a limitation
flagged in `BATDETECT2_TRAINING.md`. Keeping 0.5 meant ~0 calls
archived even with the new confidence gate. The quality control is
primarily in `MIN_PREDICTION_CONF=0.6` against the NA-trained
classifier head.

### Historical cleanup

Purged rows below the new gate from both stores:

- Postgres: 42 rows deleted, 49 remain.
- Firestore `batDetections`: 42 docs deleted, 49 remain.

Dashboard now shows only rows that would survive today's gate, so the
advisor's "what's actually a real call?" question has one honest
answer.

### Startup log line to grep

```
Monitoring started — batdetect_threshold=0.3, min_pred_conf=0.6, segment=15s
```

