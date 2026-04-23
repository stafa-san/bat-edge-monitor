# Live Capture Pipeline — Audit, Findings, Fixes

**Date:** 2026-04-23
**Branch merged to:** `dev` + `main`
**Triggered by:** "can you check the entire live capture pipeline one more time" after the Pi deploy
**TL;DR:** audit turned up one critical issue (45 % capture dead time) and two medium-priority issues (over-strict classifier + validator gates). All three fixed, documented, deployed.

---

## 1. The end-to-end pipeline (as of commit with this doc)

```
AudioMoth USB (384 kHz native — hardware-only gain control on the
    device buttons; no software knobs)
  ↓ ALSA plughw resamples once
15-second WAV @ 256 kHz mono int16 (SAMPLE_RATE env)
  ↓ producer → asyncio.Queue(maxsize=3) → consumer  (NEW)
  ↓ HPF @ 16 kHz, order 4 (analysis-only, archived WAV unchanged)
  ↓ BatDetect2 (UK-trained, diagnostic det_threshold=0.1)
  ↓ gate: user_threshold (DETECTION_THRESHOLD=0.3)
  ↓ Groups classifier (retrained NA head — 5 classes: EPFU_LANO, LABO, LACI, MYSP, PESU)
  ↓ gate: min_pred_conf (MIN_PREDICTION_CONF=0.3 — was 0.6)
  ↓ gate: FM-sweep shape filter (slope, low-band ratio, R²)
  ↓ gate: audio validator (min_rms=0.002 — was 0.005; SNR 10 dB; burst_ratio 3.0)
  ↓ storage tiering (tier 1/2/3/4)
  ↓ Postgres bat_detections
  ↓ sync-service (60 s cadence) → Firestore → dashboard
```

Orthogonal to the per-segment pipeline:

- **Cold-start warm-up** — synthetic 60→25 kHz FM chirps at boot so the model exercises its detection head. See `BATDETECT2_STABILITY_FIX.md`.
- **Model-health watchdog** — logs if 20 consecutive segments have `raw_count=0` while audio RMS is above 2× validator floor.
- **Diagnostic save** — `DIAGNOSTIC_SAVE_REJECTIONS=true` copies near-miss WAVs to `/bat_audio/_diagnostic/` with the rejection reason in the filename.
- **Daily summary email** — rollup + model-health alert when `bd_raw_avg=0` over a full window.

---

## 2. The audit — what I actually checked

The post-deploy ask was essentially "are we capturing everything we should be, and is the model capable enough 24/7." I inspected:

1. **Sample-rate chain** — AudioMoth native → ALSA plughw → our in-memory array → BatDetect2 target.
2. **BatDetect2 effective config** — `detection_threshold`, `nms_kernel_size`, `max_freq`, `min_freq`, the full `api.get_config()` dump inside the running container.
3. **Capture cadence** — distance-between-timestamps in `audio_levels` over the last ~100 segments.
4. **Gate thresholds** — `DETECTION_THRESHOLD`, `MIN_PREDICTION_CONF`, `VALIDATOR_MIN_RMS`, `VALIDATOR_MIN_SNR_DB`, `FM_SWEEP_MIN_SLOPE`, and friends.
5. **Actual loop code path** — read `main.py` end to end, including error handling.
6. **Audio headroom** — 24h distribution of `rms` / `peak` / all-time max.
7. **AudioMoth USB/ALSA health** — `arecord -l`, `lsusb`, `vcgencmd get_throttled`, `/proc/asound/cards`.
8. **What "the LLM" is (spoiler: there isn't one)** — the 24/7 analysis stack is BatDetect2 (CNN `Net2DFast`) + our retrained groups-classifier head + classical DSP gates. No LLM is in the loop, which is correct for this task — LLMs have no audio-signal understanding; a purpose-built echolocation CNN is the right tool.

---

## 3. 🔴 Critical finding — ~45 % capture dead time

### The data

`audio_levels` timestamps showed a consistent **~28 s cadence** between segments while `SEGMENT_DURATION=15`. That means for every 28 s wall-clock, only 15 s was being captured — the other ~12–13 s was spent processing the previous segment (BatDetect2 + classifier + validator + FM-sweep + DB writes) **with the microphone idle**.

Sample cadence over 20 consecutive segments:

| Segment start | gap to previous |
|---|---|
| 23:07:49 | 29.7 s |
| 23:07:20 | 27.4 s |
| 23:06:52 | 27.7 s |
| 23:06:25 | 27.9 s |
| 23:05:57 | 27.6 s |

### Why this existed

The old loop in `edge/batdetect-service/src/main.py` was strictly serial:

```python
while True:
    audio_path = await capture.capture_segment(duration=15)   # blocks 15 s
    rms, peak = _compute_audio_stats(audio_path)
    rows_data, ... = _run_batdetect_with_classifier(...)      # ~12 s on Pi 5
    # DB writes, storage tiering, logs...
    await asyncio.sleep(0.5)
```

The `await` on `capture_segment` was async in name only — internally it called `subprocess.check_call(..., shell=True)`, a **blocking** call. During the 15-second arecord, the asyncio event loop was blocked, so nothing could have run concurrently even if we had asked it to.

### Impact

A bat pass timed to land in a ~12 s processing window is **completely invisible** to the system. No detection, no WAV archived, no log line. At 15 s segments + 12 s gaps, ~43 % of each cycle has no audio being captured at all. Over a full 8-hour night that's ~3 h 20 m of missed monitoring.

### The fix — producer/consumer with a bounded queue

`main.py` now runs two concurrent asyncio tasks:

```python
segment_queue: asyncio.Queue = asyncio.Queue(maxsize=3)

async def capture_producer():
    while True:
        wav, tmpdir = await capture.capture_segment(duration=segment_duration)
        await segment_queue.put((wav, tmpdir))

async def detect_consumer():
    while True:
        wav, tmpdir = await segment_queue.get()
        try:
            # BatDetect2 + classifier + validator + FM-sweep + DB writes
        finally:
            tmpdir.cleanup()

await asyncio.gather(capture_producer(), detect_consumer())
```

Supporting changes:

- `BatAudioCapture.capture_segment` switched from `subprocess.check_call` to `asyncio.create_subprocess_exec`, so the 15-second `arecord` actually yields the event loop and the consumer can process the previous segment in parallel.
- Each queued item carries its own `TemporaryDirectory` handle (the old `self._temp_dir` single-slot pattern raced when two segments were alive at once); consumer is responsible for `cleanup()`.
- Queue `maxsize=3` means we buffer up to ~45 s of audio if detection stalls; producer blocks only when the buffer is full (graceful degradation rather than unbounded memory growth).
- **Critical follow-up discovered in deploy verification**: the first producer/consumer iteration still showed the old ~28 s cadence. Root cause: the consumer's torch inference call (`_run_batdetect_with_classifier`) is synchronous and CPU-bound. Running it directly on the event loop blocked the producer's async `arecord.communicate()` wait for ~12 s per segment — the subprocess was finishing but asyncio couldn't process the "subprocess done" event until the sync block returned. Fix: wrap torch inference in `asyncio.to_thread(...)` so the event loop stays live while the CNN runs. Second deploy confirmed cadence dropped to the expected ~15 s.

### Expected post-fix behaviour

Steady state:

```
t=0–15:   producer records segment A | consumer idle
t=15–30:  producer records segment B | consumer processes A
t=30–45:  producer records segment C | consumer processes B
...
```

Capture duty cycle goes from ~55 % → ~100 %. Gap between timestamps in `audio_levels` drops from ~28 s to ~15 s.

### Backpressure

If detection ever runs *slower* than capture (unlikely on Pi 5 but possible under thermal throttling), the queue fills. Once full, `producer.put()` blocks for a few seconds until the consumer drains one item. That's strictly better than the old behaviour — capture still runs, just not continuously. We don't drop items; we apply back-pressure.

---

## 4. 🟡 Medium finding — classifier confidence gate too strict (0.6 → 0.3)

### Why 0.6 was wrong

Our `groups_model.pt` head sits on top of a UK-trained BatDetect2 backbone. UK-trained backbones are known out-of-distribution on Ohio bats — the April-17 training notes show 83.4 % PESU accuracy but the backbone itself labels every Ohio bat as a European species before the groups head re-maps it.

Setting `MIN_PREDICTION_CONF=0.6` meant real Ohio calls where the classifier was 40–55 % confident were being rejected. There was no data supporting 0.6 specifically — the value was inherited from a pre-retrain era. Meanwhile `DETECTION_THRESHOLD` had already been lowered to 0.3 for the BatDetect2 stage for the same reason (see `DETECTION_TUNING_PLAYBOOK.md`). It was inconsistent.

### The fix

- `edge/docker-compose.yml`: `MIN_PREDICTION_CONF` default `0.6` → `0.3`
- `functions/main.py` (CF, for offline WAV analysis parity): `min_pred_conf` default `0.6` → `0.3`

### Safety net

With `min_pred_conf=0.3`, any classifier output above 30 % passes into the shape + validator gates. False positives there are caught by:

1. **FM-sweep shape filter** — rejects broadband clicks (rain, insects).
2. **Audio validator** — SNR + burst-ratio sanity check.
3. **Training-distribution threshold** — BatDetect2's own training threshold (retained as documentation in `CLASSIFIER_TRAINING_DET_THRESHOLD`).

Tuning refinement once we have real field data: plot `prediction_confidence` histogram for human-verified-true-positive rows vs. human-verified-false-positive rows, pick the threshold that maximises F1.

---

## 5. 🟡 Medium finding — validator `min_rms` too strict (0.005 → 0.002)

### The arithmetic

The audio validator runs on the whole 15 s segment, not on the per-detection bounding box. So a genuinely real but brief bat call gets RMS-averaged across 15 000 ms of mostly-silence:

| Scenario | Call peak | Silence noise | Whole-segment RMS | Old (0.005) | New (0.002) |
|---|---|---|---|---|---|
| Close bat (5 m) | 0.30 | 0.002 | ≈ 0.0078 | ✅ passes | ✅ passes |
| Medium (15 m) | 0.10 | 0.002 | ≈ 0.0028 | ❌ rejected | ✅ passes |
| Distant (30 m) | 0.05 | 0.002 | ≈ 0.0013 | ❌ rejected | ❌ rejected |

Math: `RMS² ≈ (t_call / t_segment) · peak² + (t_silence / t_segment) · noise²`, for a 10 ms call in a 15 000 ms segment.

So at `min_rms=0.005` we were silently rejecting **medium-range** bats that BatDetect2 had already correctly identified. The validator was designed to catch silence segments that confuse the classifier — `min_rms=0.002` is just above the observed ambient noise floor (0.0018 over 24 h) and still does that job.

### The fix

- `edge/docker-compose.yml`: `VALIDATOR_MIN_RMS` default `0.005` → `0.002`
- `functions/main.py` (CF): matches.

### What still protects against false positives

- `min_snr_db=10` — a real bat call has substantially more in-band energy than noise.
- `min_burst_ratio=3.0` — bat calls are short bursts, not sustained noise.
- FM-sweep shape filter — the final arbiter of "is this a real echolocation pulse."

### Future refinement (not shipped)

The better fix long-term is to run the validator on **just the detection's bounding-box audio** (e.g., the 8 ms window around each detected call) rather than the whole segment. At that scale the math changes completely and 0.005 would again be the right floor. Punted for now because it's a bigger refactor and the threshold drop addresses the immediate false-negatives.

---

## 6. ✅ What's actually going right (unchanged by this audit)

- **Frequency coverage**: 128 kHz Nyquist at 256 kHz sampling covers all NA bat species (max ~120 kHz).
- **Sample rate chain**: one resample step (384→256 kHz via ALSA plughw), no lossy re-interpolation.
- **HPF at 16 kHz** is *below* the bat band — removes fans/wind/voices without touching call content.
- **AudioMoth hardware**: all-time RMS max 0.0205 + peak max 0.9072 proves the mic CAN capture loud transients; no throttling, clean USB enumeration.
- **Warm-up + watchdog + golden-file test** from the earlier stability fix are still running and healthy.
- **Diagnostic save** on the Pi means near-miss rejections are preserved for forensic review.

---

## 7. Not shipped, noted for later

1. **Detection-window validator** — see §5.
2. **Hardware gain review** — AudioMoth USB mode uses button-selected gain presets (low / med / high / very high). Ask Dr. Johnson whether the setup is at the right preset for the expected detection distance. We have no way to check this from software.
3. **Retrain BatDetect2 backbone on NA data** — covered in `RETRAINED_NA_DETECTOR_PLAN.md`. This is the real long-term fix; thresholds become properly tunable only when the backbone sees its own training distribution at inference time.
4. **Canary cron** — feed a synthetic WAV through the live pipeline hourly and alert if detection count drops to zero. Previously discussed; not shipped.
5. **Promote watchdog from log-only to auto-restart** — once we've seen it behave in real field audio and are confident the thresholds aren't over-sensitive.

---

## 8. Files changed by this audit

| File | Change |
|---|---|
| `edge/batdetect-service/src/main.py` | Refactored serial while-loop into `capture_producer()` + `detect_consumer()` running under `asyncio.gather`. `BatAudioCapture.capture_segment` now returns `(path, tempdir)` and uses `asyncio.create_subprocess_exec`. |
| `edge/docker-compose.yml` | `MIN_PREDICTION_CONF` default `0.6` → `0.3`; `VALIDATOR_MIN_RMS` default `0.005` → `0.002`; rationale comments added inline. |
| `functions/main.py` | Matching CF defaults for offline WAV analysis parity. |

## 9. How to verify after deploy

```bash
# On Pi, ~2 minutes after `docker compose up -d batdetect-service`:
docker compose exec -T db psql -U postgres -d soundscape -c "
SELECT to_char(recorded_at, 'HH24:MI:SS') AS t,
       round(EXTRACT(EPOCH FROM (recorded_at - LAG(recorded_at) OVER (ORDER BY recorded_at)))::numeric, 1) AS gap_s
FROM audio_levels ORDER BY recorded_at DESC LIMIT 10;"
```

Expected: `gap_s` column drops from ~28 to ~15. That's the critical fix working.

```bash
docker compose logs batdetect-service 2>&1 | grep 'No bat calls detected\|bat call(s) detected\|rejected by' | tail -20
```

Expected: segment numbers continuing to increment, mix of heartbeat + rejection + detection lines, no `MODEL-HEALTH WARNING`.
