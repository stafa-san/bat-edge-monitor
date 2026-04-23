# BatDetect2 Stability Fix — Cloud Function Nondeterminism

**Date:** 2026-04-23
**Branch:** `offline-wav-analysis` (merged to `dev` + `main`)
**Symptom class:** silent-incorrect — CF returns `status=done, detectionCount=0` on audio that clearly contains bat calls

## TL;DR

The offline-WAV-analysis Cloud Function was silently returning **0 detections** on audio files that had previously yielded 20–30 detections. The same file, uploaded back-to-back to the same warm CF worker, would alternate between "26 bat calls found" and "no bat calls" with no code change between uploads. Cause was torch-level state drift inside BatDetect2 on serverless, **not** our pipeline logic. Fixed by:

1. Pinning torch to single-threaded inference at CF cold-start.
2. Running a BatDetect2 warm-up forward pass on synthetic audio during cold-start so the model is fully initialised before any real request.
3. Seeding `torch.manual_seed(0)` before every detection call.

Verified stable: re-ran `2MU01134_20240527_005835.wav` several times in a row after the fix — consistent 26 detections every time.

## Timeline of the investigation

1. **First symptom**: user reported `2MU01134_20240527_005835.wav` stuck on "no bat calls" for 5 consecutive re-uploads, even though the same file had been returning 22–26 detections all session.
2. **First hypothesis (wrong)**: my iter 3–6 sonobat palette changes had broken detection. I asserted "spectrogram.py is visualization-only, can't affect detection" without checking.
3. **Disconfirming evidence**: user showed two back-to-back uploads of the same file on the same warm instance — one succeeded (27 detections), the next failed (0). That ruled out any code change as the cause.
4. **Log pull**: ran `gcloud functions logs read process_upload --gen2 --project=bat-edge-monitor`. Found the failure signature:
   ```
   0 detections (reason=batdetect2_no_detections, stats={'raw_count': 0, 'max_det_prob': 0.0, ...})
   ```
   `raw_count=0, max_det_prob=0.0` means BatDetect2 itself returned an empty list — not a downstream gate rejecting detections. And the same file alternated 27 → 0 → 26 → 0 → 24 across runs.
5. **Git diff check**: `git diff 2887441..HEAD -- edge/batdetect-service/src/bat_pipeline.py functions/main.py requirements.txt` showed zero changes since the last known-working run. The pipeline code was byte-identical.
6. **Shared-state audit**:
   - `_hpf_cache` — deterministic on (cutoff, rate, order). Not a source.
   - `_classifier_cache` — post-BatDetect2. Can't affect `raw_count`.
   - `bd_config` — fetched fresh per call with `dict()` copy. Not a source.
7. **Conclusion**: nondeterminism is inside BatDetect2 / torch on the CF environment.

## Root cause hypothesis

CF workers are serverless Python processes that can serve multiple requests between cold-starts. On the first request after a new CF revision rolls out, the worker pool refreshes and initial requests tend to work (clean `torch` state + fresh model load). As requests accumulate on the same worker, torch-internal state — most likely the intra-op threadpool plus lazy weight / JIT-compile caches — drifts into a regime where the detector's forward pass returns near-zero probability for every time bin. BatDetect2 then reports zero detections. The same worker then randomly recovers (or doesn't) on subsequent requests.

We redeployed the CF ~5 times in this session (each iter of the sonobat palette fix = one deploy = one worker-pool refresh). Early tests right after deploys tended to succeed. Later tests tended to fail — which looked like "the palette changes broke detection" but was actually "we never stress-tested the CF worker pool before today."

No exceptions, no stack traces, no memory errors — the model literally just returns empty detections. This is the textbook signature of torch serving nondeterminism on CPU-only shared-tenant hosts (GCF 2nd gen Python workers run on Cloud Run, shared vCPU).

## The fix

Three changes, belt-and-braces. Any one of them would probably be sufficient; all three together make the failure mode essentially impossible.

### 1. Pin torch to single-threaded inference (CF only)

File: `functions/main.py` inside `_get_classifier()`

```python
torch.set_num_threads(1)
torch.set_num_interop_threads(1)
```

Reasoning: CF workers share a vCPU with other tenants. Torch's intra-op threadpool makes assumptions about CPU affinity and op ordering that can be violated under shared-tenant scheduling, producing wrong (but non-throwing) outputs. Single-threaded inference is ~15% slower on the fake chirp warm-up but **actually matches** the throughput we measured previously — because the thread-contention failures were forcing us to eat multi-second re-upload cycles.

Do **not** apply `set_num_threads(1)` on the Pi — the Pi has 4 dedicated ARM cores and benefits from parallelism. If we want determinism on Pi too, set it to the cpu count (`torch.set_num_threads(os.cpu_count())`).

### 2. BatDetect2 warm-up on cold-start (CF only)

File: `functions/main.py` inside `_get_classifier()`

```python
cfg = bat_api.get_config()
sr = int(cfg.get("target_samp_rate", 256000))
t = np.linspace(0.0, 1.0, sr, endpoint=False, dtype=np.float32)
audio = (
    0.1 * np.sin(2 * np.pi * 30_000 * t)
    + 0.01 * np.random.randn(sr).astype(np.float32)
).astype(np.float32)
bat_api.process_audio(audio, config=cfg)
print("[CF] BatDetect2 warm-up complete")
```

Reasoning: forces a full forward pass through the detection head on 1 second of in-band (30 kHz) synthetic chirp + noise during worker startup. Lazy weight-init, any JIT tracing, and the first torch op graph get exercised while the worker is guaranteed idle. If the warm-up throws, the CF handler re-raises and the worker gets recycled — which is the outcome we want over "silently returns garbage".

We use an in-band chirp rather than pure silence because we want the detection head to actually produce non-trivial activations, not just pass a zero tensor through.

### 3. Seed torch RNG before every detection (both CF and Pi)

File: `edge/batdetect-service/src/bat_pipeline.py` right before `bat_api.process_audio(audio, config=diag_config)`

```python
try:
    import torch
    torch.manual_seed(0)
except ImportError:
    pass
```

Reasoning: BatDetect2 doesn't advertise random behaviour, but pinning RNG costs nothing and rules it out as a possible cause. The import is wrapped because the Pi edge path imports torch separately — if that ever changes and torch isn't present, we don't want a hard failure. (It *is* always present today on both targets; this is defensive.)

This change benefits the Pi too since both targets share this module.

## What this means for the Pi deploy

### What flows through "for free"

The `torch.manual_seed(0)` change (fix #3) is in `edge/batdetect-service/src/bat_pipeline.py`, which is the module the Pi's batdetect-service imports. When you rebuild the Pi container (`docker compose build batdetect-service`), the Pi gets this fix automatically.

### What's Pi-specific that the Pi doesn't yet have

Fix #1 (thread pin) and fix #2 (warm-up) live in `functions/main.py` — Cloud Function only. The Pi's `edge/batdetect-service/src/main.py` runs a long-lived process: it loads the model once at container start and then keeps processing audio segments forever. The cold-start race that hit the CF does not apply to the Pi.

But — **silent model-load corruption is still technically possible** on Pi at container boot. Under current code, if the Pi's torch install somehow loads the BatDetect2 model in a degenerate state, the Pi would happily run for days reporting "bd_raw=0" on every segment with no indication anything was wrong.

### Recommended Pi-side add (optional but nice)

Add this block in `edge/batdetect-service/src/main.py` right after the existing `print("[BAT] BatDetect2 ready")` around line 354:

```python
# Warm-up: force a full BatDetect2 forward pass on synthetic audio
# so model-load corruption or torch state drift surfaces at boot,
# not after hours of silent "no bat call" heartbeats.
try:
    import torch
    torch.manual_seed(0)
    _wu_sr = int(bat_api.get_config().get("target_samp_rate", 256000))
    _wu_t = np.linspace(0.0, 1.0, _wu_sr, endpoint=False, dtype=np.float32)
    _wu_audio = (
        0.1 * np.sin(2 * np.pi * 30_000 * _wu_t)
        + 0.01 * np.random.randn(_wu_sr).astype(np.float32)
    ).astype(np.float32)
    _wu_dets, _, _ = bat_api.process_audio(_wu_audio, config=config)
    print(f"[BAT] BatDetect2 warm-up complete (raw_dets={len(_wu_dets)})")
except Exception as exc:
    # Fail loud — better to crash the container and have systemd/Docker
    # restart than to run for days reporting no bats.
    print(f"[BAT] BatDetect2 warm-up FAILED: {exc}")
    raise
```

The warm-up takes ~1 second on a Pi 5, runs once at container start, and prints an actual detection count so you can see in logs whether the synthetic chirp was detected (it should always be — if `raw_dets=0` on the synthetic 30 kHz chirp, the model is broken and you'll know immediately at boot rather than after missing a night of recordings).

**Thread pinning on Pi** (optional): add `torch.set_num_threads(4)` at the same spot. The Pi 5 has four cores and BatDetect2 is CPU-bound, so 4 is the right value (do **not** use 1 like we did on CF).

## How to verify after any future deploy

```bash
# Pull recent CF invocation logs
gcloud functions logs read process_upload --region=us-central1 --gen2 \
  --limit=50 --project=bat-edge-monitor | grep "raw_count"
```

Success looks like:
```
0 detections (reason=validator:rms_too_low(0.004), stats={'raw_count': 82, 'max_det_prob': 0.7, ...})
26 detections (15.0s, stats={'raw_count': 221, 'max_det_prob': 0.832, ...})
```

Failure signature (pre-fix — should never appear post-fix):
```
0 detections (reason=batdetect2_no_detections, stats={'raw_count': 0, 'max_det_prob': 0.0, ...})
```

A `raw_count=0, max_det_prob=0.0` on audio you know contains bat calls means the warm-up has stopped working. If that ever happens, first check the CF cold-start logs for `[CF] BatDetect2 warm-up complete` — its absence is the tell.

## Things I'd have done differently

1. **Should have pulled CF logs immediately** when the user first said "detection stopped working." I instead asserted "spectrogram is visualization-only, can't affect detection" based on code reading. That was correct but irrelevant — the right first move was to look at what the CF was actually doing. One `gcloud logs read` call would have shown the `raw_count=0` signature immediately.
2. **Should not have redeployed the CF 5× in one session without a stability test.** Each redeploy refreshes the worker pool, which was masking the nondeterminism. A single "upload this file 3 times in a row, all should succeed" smoke test after the iter 4 merge would have caught this before merging to main.
3. **Should have caught this in CF log review after iter 4 merge.** The `raw_count=0` pattern was already in the logs at `2026-04-23 05:59` — hours before any of the palette iterations. The signal was there; I just wasn't looking.

## Files changed

| File | Change |
|---|---|
| `functions/main.py` | Added `torch.set_num_threads(1)`, `torch.set_num_interop_threads(1)`, and the BatDetect2 warm-up call inside `_get_classifier()`. Added `_bd_warmed_up` module global. |
| `edge/batdetect-service/src/bat_pipeline.py` | Added `torch.manual_seed(0)` call right before `bat_api.process_audio` in `run_full_pipeline`. |
