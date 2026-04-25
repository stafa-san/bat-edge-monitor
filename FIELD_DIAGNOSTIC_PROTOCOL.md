# Field Diagnostic Protocol — converging on the real cause of zero-detections

**Started:** 2026-04-23 (initial deploy)
**Latest update:** 2026-04-24 (after 24+ h of zero detections + advisor-WAV diagnostic)

This is the running playbook of what we know, what we think, what we're trying,
and how to move forward — written so any session can pick up where we left off.

Companion docs:
- [`AUDIO_CAPTURE_AUDIT.md`](AUDIO_CAPTURE_AUDIT.md) — capture-chain stages
- [`PIPELINE_AUDIT_AND_FIXES.md`](PIPELINE_AUDIT_AND_FIXES.md) — gate tuning history
- [`BATDETECT2_STABILITY_FIX.md`](BATDETECT2_STABILITY_FIX.md) — CF nondeterminism
- [`AUDIO_VALIDATOR.md`](AUDIO_VALIDATOR.md) — validator math
- [`ZERO_DETECTIONS_RUNBOOK.md`](ZERO_DETECTIONS_RUNBOOK.md) — morning triage
- [`RETRAINED_NA_DETECTOR_PLAN.md`](RETRAINED_NA_DETECTOR_PLAN.md) — long-term retrain

---

## The current situation in one paragraph

After 24 + hours of live capture on the Pi at the field site (gain "high",
hardware HPF 8 kHz, native 384 kHz), `bat_detections` has **zero rows**.
Hardware is verified healthy, capture chain is verified clean (see
`AUDIO_CAPTURE_AUDIT.md`), noise floor is *quieter* than training data,
gates are functioning correctly. Yet nothing is being recorded. Two upload-WAV
samples from the advisor (his 2021 wildlife-detector recordings, **not**
training data and **not** AudioMoth) were both flagged "no bat calls" by the
offline-analysis CF — but our forensic dive showed BatDetect2 *did* see
hundreds of bat-like emissions in those files. The pipeline is *working*,
not broken; it's just tuned strict for a specific signal profile.

---

## Today's key forensic findings (2026-04-24)

### Advisor's two test WAVs (not training data, not AudioMoth)
Files: `20210326_235900T.WAV`, `20210327_000100T.WAV`. Recorded on his wildlife
detector (different mic, different gain, different recording chain) ~5 years
ago. He has not yet confirmed species. Both flagged `no bat calls /
validator:rms_too_low(0.0016)` by the offline pipeline.

### Direct BatDetect2 run on those files (threshold 0.05)

| File | Total raw detections | Highest det_prob | ≥ 0.30 (user thr) | ≥ 0.50 |
|---|---|---|---|---|
| `20210326_235900T` | **197** | 0.560 | **3** | 1 |
| `20210327_000100T` | **276** | 0.701 | **6** | 3 |
| Reference (`005835` — known-bat) | 318 | 0.836 | **186** | 117 |

**The detector finds the bats. The gates reject because amplitude is too low.**

### Spectrogram visual confirmation

Both advisor files show clear vertical FM-sweep streaks in 15–30 kHz, distributed
across the 28 s clip — real bat echolocation pulses, just weak. REF_005835 shows
much brighter sweeps with broader vertical extent (close-pass loud bat).

### What the numbers tell us

| Metric | Advisor avg | REF | Interpretation |
|---|---|---|---|
| Whole-file RMS | 0.0021 | 0.014 | Advisor 7× quieter |
| Peak amplitude | 0.10 | 0.91 | Advisor 9× quieter on peaks |
| Loudest 10 ms window | −39 dBFS | −15 dBFS | 24 dB quieter — calls themselves are weak |
| Top-1 % windows / total | 30 / ~2900 | 15 / 1500 | Similar density of "loud events" |
| Top BD-class | `Rhinolophus hipposideros` | `Eptesicus serotinus` | Both UK; OOD relabelling expected |

The advisor recordings have **the same density of bat events as the reference**
(top-1% windows per second is similar) but at **9× lower amplitude**. This is
exactly the signature of either:
- (a) Distant flyovers (bats far from mic)
- (b) Lower preamp gain on the wildlife-detector vs AudioMoth-high

We don't know which without metadata from advisor.

---

## Decision tree — when to lower thresholds vs not

```
Q: Is the system rejecting real bat calls?
│
├─ For Pi live capture: We don't know yet — hasn't seen any bats yet.
│  ├─ If audio_levels mid_p95 stays at noise floor (~0.001) overnight
│  │  → bats just didn't fly (weather). Keep thresholds.
│  ├─ If mid_p95 spikes (>0.005) but bd_raw_avg stays low
│  │  → captured something but BD missed it. Investigate.
│  └─ If mid_p95 spikes AND bd_raw_avg climbs but kept=0
│     → gates over-rejecting. Consider lowering thresholds.
│
├─ For offline upload analysis: Advisor's files reject as "no bat calls"
│  ├─ If advisor's files are representative of real Ohio bat audio
│  │  → gates are too strict, lower
│  └─ If advisor's files are unusually faint (different recorder, lower gain,
│     distant flyovers) → don't tune Pi based on them. Get more diverse samples.
│
└─ Long-term: UK-trained backbone misclassifies NA bats consistently
   → retrain (RETRAINED_NA_DETECTOR_PLAN.md). Real fix.
```

The Pi's settings are correct **for AudioMoth-on-high-gain capturing close
passes**. They may be too strict for **distant or lower-gain captures**. We
don't yet know which our deploy site produces.

---

## Tonight's plan: Permissive Night Mode (PNM)

### Why

After 24+ h of zero detections we don't actually know what the Pi is hearing.
The only way to find out is to relax the gates **just for tonight**, capture
permissively, then post-hoc filter false positives in the morning. This
trades precision for recall *temporarily*. Diagnostic-save remains on, so
near-miss rejections are still preserved for review.

### What changes

| Setting | Default (committed) | PNM (tonight only) | Why |
|---|---|---|---|
| `DETECTION_THRESHOLD` | 0.30 | **0.15** | BatDetect2 was finding 197 events at 0.05 in advisor files. 0.15 is enough to filter pure noise but lets weak bats through. |
| `VALIDATOR_MIN_RMS` | 0.002 | **0.0008** | Above the AudioMoth's noise floor (0.0018) but below the advisor-file post-HPF level (0.0016). |
| `MIN_PREDICTION_CONF` | 0.30 | **0.20** | Classifier sometimes lands at 0.4–0.5 on real bats due to UK-backbone OOD. |
| `FM_SWEEP_MIN_R2` | 0.20 | **0.10** | Allows less-perfect linear sweeps through. CF (constant-freq) calls have low R²; some Ohio species use QCF tails. |

These are stacked relaxations — each one alone is small, together they're
significant. We **expect false positives**.

### What stays the same

- `FM_SWEEP_ENABLED=true` — the slope direction check is still a real signal
- `VALIDATOR_ENABLED=true` — SNR and burst-ratio checks still active
- `DIAGNOSTIC_SAVE_REJECTIONS=true` — every near-miss gets a WAV
- Storage tiering, OneDrive sync, all schema unchanged

### How PNM is applied

Per-Pi `.env` override (not committed to repo). Edit `~/bat-edge-monitor/edge/.env`:

```ini
# Permissive Night Mode — added 2026-04-24, see FIELD_DIAGNOSTIC_PROTOCOL.md
DETECTION_THRESHOLD=0.15
VALIDATOR_MIN_RMS=0.0008
MIN_PREDICTION_CONF=0.2
FM_SWEEP_MIN_R2=0.1
```

Apply: `docker compose up -d batdetect-service`. No rebuild, no source change.
Startup log will print active thresholds — verify before walking away.

### How to revert PNM

```bash
cd ~/bat-edge-monitor/edge
# Remove the four PNM lines from .env (or comment them out)
# Then bring the service back up:
docker compose up -d batdetect-service
```

The committed defaults from `docker-compose.yml` (0.30 / 0.002 / 0.3 / 0.2)
will then re-apply.

---

## Tomorrow morning — post-hoc filter pass

A separate analysis script (see `edge/scripts/triage_pnm_night.py`) can be run
the morning after a PNM night. It pulls every detection from the window,
joins with `audio_levels` for context, and emits a sorted CSV the user can
review:

```bash
docker compose exec -T batdetect-service \
  python /app/edge/scripts/triage_pnm_night.py --window-hours 14 \
  > pnm_review.csv
```

Columns include: detection_time, predicted_class, prediction_confidence,
detection_prob, low_freq, high_freq, duration_ms, audio_path,
plus the bd_top_class and bat_band_mid_rms for the parent segment.

What to flag manually as likely false positive:
- `prediction_confidence` < 0.4 with no bat-band activity in parent segment
- `bat_band_mid_rms` below 0.0015 (segment was nearly silent)
- `duration_ms` < 2 ms (too short to be a real call)
- `bd_top_class` is `Rhinolophus *` (UK horseshoes — almost certainly OOD-mislabel)

What looks like a real bat:
- `prediction_confidence` ≥ 0.5
- `bat_band_mid_rms` ≥ 0.005 in parent segment
- `low_freq` and `high_freq` are sensibly different (FM sweep present)
- `bd_top_class` is one of the European FM-sweep species (Eptesicus, Pipistrellus)

The morning script will pre-flag obvious cases and leave the borderline ones
for human eyes.

---

## Knowledge questions (the user's #2)

### Q: Sample rate — 256 kHz instead of 384 kHz?

**Pi USB-mic mode**: 384 kHz is forced. The AudioMoth USB descriptor
advertises only `tSamFreq[0]=384000`. Asking ALSA for 256 kHz triggers a
linear resampler with no anti-alias filter (we documented this and that's
why we switched to native 384 kHz). **256 kHz on Pi USB is strictly
worse than 384 kHz**. Don't change.

**AudioMoth standalone mode** (advisor's second device): 256 kHz can be set
natively (no resampling). Saves SD-card space (33% smaller files), still
covers the entire NA bat band (Nyquist 128 kHz > max bat call ~120 kHz).
For standalone, 256 kHz is a perfectly good choice — but no measurable
detection benefit over 384 kHz either.

### Q: Lower the AudioMoth gain? medium / med-high instead of high?

Gain settings:
- low (~15 dB), med-low (~20 dB), med (~30 dB), med-high (~35 dB),
  high (~40 dB), very-high (~45 dB)

Reasons to lower gain:
- If recordings clip (peak hits 1.0 on loud passes) — **not the case here**;
  REF peak is 0.91 maximum, advisor peaks are 0.10
- If the "high"-gain noise floor sounds louder than training — **also not the
  case**; we measured Pi noise floor 5 dB *quieter* than training
- If there's tonal interference being amplified — **not present**; tonality
  ratio is 3–4× (broadband noise, no narrowband sources)

Reasons to keep gain at "high":
- Distant bats need amplification to clear the noise floor
- Lowering gain **does not improve SNR** — both signal and noise scale
- Lower gain hurts validator's whole-segment RMS check (already a tight margin)

**Recommendation: keep gain at "high" for at least one full bat-active night
under PNM.** Once we have detection data, we can revisit if needed. Do NOT
change tonight — would confound the PNM experiment.

### Q: Should we adjust the hardware HPF (currently 8 kHz)?

**No.** The 8 kHz HPF kills wind/AC rumble below 8 kHz, has zero attenuation
in the bat band (>15 kHz). Already verified math in `AUDIO_CAPTURE_AUDIT.md`.

---

## Metadata to request from advisor (the user's #5)

For the future retrain and to interpret current diagnostic data:

1. **Recorder model** for the 2021 WAVs (Wildlife Acoustics SM4BAT? Echo Meter?
   Pettersson D500X?)
2. **Gain setting** used during recording
3. **Microphone type** (omni vs directional? Built-in vs external?)
4. **Recording site** characteristics (open field, near water, near roost, etc.)
5. **Confirmed species** for the two specific test files
6. **More test files** ideally:
   - Several recordings of EACH NA species (EPFU, LANO, LABO, LACI, MYSP, PESU)
   - At various distances (close, mid, far) for each species
   - Some "negative" examples (silent night, insect chorus, mechanical noise)
   - Ideally recorded with the same AudioMoth firmware on similar hardware
     so the recording chain matches our Pi
7. **Existing labels / ground truth files** — bounding boxes + species per call
   from anyone's analysis tool (BatExplorer, Kaleidoscope, Audacity labels)

---

## Workspace state (what's deployed where)

| Component | Branch / commit | Notes |
|---|---|---|
| GitHub `main` | `5a683d1` (today) | Stability fix + per-band RMS + runbook |
| Cloud Function | Deployed today | `validator_min_rms=0.002`, `min_pred_conf=0.3` |
| Pi `bat-edge-monitor` | `cd97f38` synced | Native 384 kHz capture, schema migrated |
| Pi `batdetect-service` runtime | Up 6+ h | Last warm-up: `raw_dets=10` ✅ |
| Pi `.env` overrides | `ENABLE_*=true` (5 of them) | About to add 4 PNM lines |

---

## Reverse-chronology of changes today (newest first)

1. **PNM env-vars added to Pi `.env`** (this update) — 4 thresholds relaxed
2. **Per-band RMS + bd_top_class telemetry** added to `audio_levels` schema
3. **Native 384 kHz capture** — fix for ALSA linear-resampler aliasing
4. **Producer/consumer + asyncio.to_thread** — recovered ~45 % capture dead time
5. **Validator default 0.005 → 0.002** + classifier 0.6 → 0.3 (audit-driven)
6. **Diagnostic save** of near-miss rejections to `/bat_audio/_diagnostic/`
7. **Warm-up FM-chirp** signal — fix for false-positive crash loops
8. **Pi-side stability hardening** — warm-up + watchdog + golden-file test
9. **CF stability fix** — torch threadpool pin + warm-up forward pass

If anything regresses, the rollback path follows the reverse order — start
from the most recent change and work backward until detection resumes.
