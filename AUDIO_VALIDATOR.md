# Audio Validator — Third Gate in the Detection Pipeline

Companion to [`BATDETECT2_TRAINING.md`](BATDETECT2_TRAINING.md),
[`SESSION_NOTES_2026-04-20.md`](SESSION_NOTES_2026-04-20.md), and
[`DETECTION_TUNING_PLAYBOOK.md`](DETECTION_TUNING_PLAYBOOK.md).

Implementation:
[`edge/batdetect-service/src/audio_validator.py`](edge/batdetect-service/src/audio_validator.py).

Status: live in production since 2026-04-20 late evening (PR #39).

---

## Why this exists

The groups classifier is trained to pick between five NA bat classes.
Its softmax always sums to 1, so when we feed it broadband fan noise,
silence, or wind rumble, it **must** assign a class — and the closest
match in feature space is typically LACI because low-frequency
mechanical noise sits in the same band as Hoary Bat calls. That's how
we ended up with a tier-1 archive on 2026-04-20 full of files whose
RMS was 0.0012 (functionally silent) confidently labeled LACI at
pred_conf 0.7+.

The classifier cannot say "this isn't a bat at all." The validator
does that job, after the classifier, before anything is archived or
uploaded.

## The three-gate pipeline

```
    Raw 15s WAV from AudioMoth
             │
             ▼
  ┌─────────────────────┐
  │  BatDetect2 base    │  det_prob ≥ DETECTION_THRESHOLD (0.3; was 0.5)
  └─────────────────────┘
             │  survivors
             ▼
  ┌─────────────────────┐
  │  NA-groups          │  prediction_confidence ≥ MIN_PREDICTION_CONF (0.3; was 0.6)
  │  classifier head    │
  └─────────────────────┘
             │  survivors
             ▼
  ┌─────────────────────┐
  │  Audio validator    │  RMS + SNR + burst checks
  │  (this doc)         │
  └─────────────────────┘
             │  confirmed bat
             ▼
    Postgres row + Firestore + /bat_audio + gdrive
```

Fail any gate → dropped silently. No DB row, no WAV, no dashboard,
no cloud upload. All four downstream surfaces see the same set.

## What the validator analyzes

**Band of analysis: 15 kHz – 120 kHz.** Defined in
`audio_validator.py` as `BAT_BAND_LOW_HZ = 15000` and
`BAT_BAND_HIGH_HZ = 120000`. At the Pi's 256 kHz sample rate the
effective upper bound is 120 kHz (well under Nyquist at 128 kHz).

Two of the three checks ignore sub-15 kHz content entirely; the
third (RMS) sees the full audio.

| Check | Scan range | Rejects |
| --- | --- | --- |
| **RMS floor** | Full audio | Silent segments — the 0.0012 RMS case that sparked this work |
| **Bat-band SNR** | 15–120 kHz only | Broadband uniform noise (no concentrated events) |
| **Burst ratio** | 15–120 kHz only | Steady-state noise spread evenly across the 15 s |

### Check 1 — RMS floor

```python
rms = sqrt(mean(audio**2))
if rms < min_rms: reject
```

Default: `VALIDATOR_MIN_RMS = 0.002` (canonical since 2026-04-23 —
was 0.005 originally, dropped after the live-pipeline audit; see
[`PIPELINE_AUDIT_AND_FIXES.md`](PIPELINE_AUDIT_AND_FIXES.md) §5).

Catches near-silent segments. At 0.002 it's still well above the
noise floor (observed 24 h min ≈ 0.0018), so silent sub-validator
floor segments are filtered, but medium-distance bats whose 10 ms
call gets RMS-averaged across a 15 s segment now pass through to
the SNR + burst tests instead of being rejected here. The
2026-04-20 6:59 PM silent false positive (RMS 0.0012) is still
blocked.

### Check 2 — Bat-band peak-to-median SNR

```python
Sxx = spectrogram(audio)[15_000 Hz : 120_000 Hz, :]
snr_db = 20 * log10(max(Sxx) / median(Sxx))
if snr_db < min_snr_db: reject
```

Default: `VALIDATOR_MIN_SNR_DB = 10.0`.

A real echolocation pulse is a bright, concentrated spot in the
spectrogram. Broadband noise is uniform — peak energy looks like
median energy, low SNR. Anything with less than 10 dB of contrast
inside the bat band is noise.

### Check 3 — Temporal burst ratio

```python
frame_peaks = max(Sxx, axis=0)
burst = percentile(frame_peaks, 95) / median(frame_peaks)
if burst < min_burst_ratio: reject
```

Default: `VALIDATOR_MIN_BURST_RATIO = 3.0`.

Real bat calls are transients — one or a few bright frames in an
otherwise quiet 15-second window. Mechanical drone produces roughly
equal peak energy in every frame. This check requires at least a 3×
ratio between the brightest frame and the median frame.

## Will it reject real NA bat calls?

**Not under normal recording conditions.** All five target groups
call comfortably above the 15 kHz floor:

| Group | Typical band | Margin above 15 kHz |
| --- | --- | --- |
| LACI (Hoary) | 18–35 kHz | 3 kHz (thinnest) |
| EPFU_LANO (Big Brown / Silver-haired) | 25–55 kHz | 10 kHz |
| LABO (Eastern Red) | 35–55 kHz | 20 kHz |
| MYSP (Myotis spp.) | 35–80 kHz | 20 kHz |
| PESU (Tri-colored) | 40–55 kHz | 25 kHz |

LACI is the closest to the floor; its lower harmonics can dip to
~18 kHz. We put the validator's band at 15 kHz specifically to give
LACI a 3 kHz cushion. A real LACI pulse has its peak energy around
22–25 kHz, well inside what the validator scans.

### Where it *could* mis-reject

1. **Very distant or very quiet real call** — a faint bat on the
   edge of microphone range might have RMS below 0.005. The RMS
   floor is the most likely source of false rejections in practice.
2. **Single brief call in an otherwise silent 15 s** — should be
   fine (the burst test catches single-frame transients), but an
   extremely short call (< 1 ms) might not register strongly.
3. **Species below 15 kHz** — free-tailed bats (molossids) call as
   low as 10 kHz and would be clipped. Not relevant for Ohio.

## Tuning — how to relax if it's too strict

Every rejection logs a reason:

```
[BAT] #42 | rejected by validator:rms_too_low(0.0023)
[BAT] #43 | rejected by validator:snr_too_low(6.4dB)
[BAT] #44 | rejected by validator:no_burst(1.8x)
```

If you spot-check a "rejected" spectrogram and see a real call the
validator missed, adjust the corresponding env var:

| Env var | Current default | Lower to allow more through |
| --- | --- | --- |
| `VALIDATOR_MIN_RMS` | **0.002** (was 0.005) | 0.001 for extremely quiet sites (risk: RMS check is nearly no-op) |
| `VALIDATOR_MIN_SNR_DB` | 10.0 | 8.0 for noisy sites |
| `VALIDATOR_MIN_BURST_RATIO` | 3.0 | 2.0 if losing short transients |
| `VALIDATOR_ENABLED` | `true` | `false` disables entirely |

All four are read from env in `batdetect-service/main.py`. Change the
value in `edge/docker-compose.yml` (or `.env`) and run:

```bash
cd ~/bat-edge-monitor/edge
docker compose up -d batdetect-service
```

No rebuild required — the values are read at container startup.
Startup log confirms the active config:

```
[BAT] Audio validator enabled — min_rms=0.002, min_snr_db=10.0, min_burst_ratio=3.0
```

## Why this is the right layer

We considered two alternatives before settling on this validator:

- **Tier-3 metadata-only logging** (proposed in
  `DETECTION_TUNING_PLAYBOOK.md` section 3.1) — would have stored
  every sub-threshold detection in Postgres for later analysis,
  without fixing the false-positive stream. Good for diagnostics;
  doesn't solve the user problem ("my gdrive is full of noise").
- **Retrain the classifier on a noise class** — proper long-term
  solution but requires weeks of site data + GPU retrain + model
  swap. Not a this-week fix.

The signal-processing validator gets us the user-facing behavior the
classifier can't provide, with no ML complexity and no training-data
dependency. It's also a clean layer boundary: the classifier answers
"which species?" and the validator answers "was that actually a
bat?" — two different jobs, two different tools.

## Relationship to the 8 kHz vs 16 kHz AudioMoth HPF question

The validator's bat-band checks look in 15–120 kHz, so whatever
energy the AudioMoth lets through below 15 kHz is **invisible** to
the SNR and burst tests. This means the hardware HPF setting matters
much less for pipeline correctness than it did before the validator
existed.

What the AudioMoth HPF still affects:

- **Cleanliness of archived WAVs** — with 8 kHz HPF the saved file
  contains 8–16 kHz mechanical rumble. Advisors inspecting WAVs in
  Audacity still hear that. A 16 kHz hardware HPF would produce
  cleaner spectrograms for manual review.
- **File size** — negligible difference.
- **RMS inflation** — sub-bat energy bumps RMS slightly. Not enough
  to fool the RMS check (real calls are 10–100× louder than
  broadband sub-bat content), but present.

Bottom line: raising the hardware HPF from 8 → 16 kHz is still a
good quality-of-life change for manual inspection; it's no longer
critical for keeping noise out of the archive.

## Known limits

- **Not species-aware.** The validator checks "is this a bat call?"
  not "is this the species the classifier claimed?" A MYSP call
  that scattered up to 80 kHz still looks like "structured energy in
  the bat band" and passes — even if the classifier mislabeled it
  as LACI. The validator reduces false-positive noise but doesn't
  catch species misclassification.
- **Fixed band.** If you ever deploy in an area with molossid bats
  (free-tailed species, 10–15 kHz), you would need to lower
  `BAT_BAND_LOW_HZ` in the Python source. Not exposed as env var
  because it's not relevant for the 5 Ohio groups.
- **No spectral shape matching.** A loud broadband transient that
  happens to have a peak in the bat band will pass the SNR test.
  In practice, real-world noise sources that produce sharp bat-band
  peaks are rare (cicadas, some bird alarm calls) and would
  generally fail the burst test or be filtered upstream.

## Log line taxonomy for quick grepping

```
# successful detection
-> tier 1/LACI (max det_prob=0.712, max pred_conf=0.814) -> /bat_audio/...

# rejected at validator
[BAT] #123 | rejected by validator:rms_too_low(0.0023)
[BAT] #124 | rejected by validator:snr_too_low(7.8dB)
[BAT] #125 | rejected by validator:no_burst(2.1x)

# nothing fired (quiet segment)
[BAT] #130 | No bat calls detected (listening...)
```

To see validator rejections over the last hour:

```bash
docker logs edge-batdetect-service-1 --since 1h | grep "rejected by validator"
```

To count by reason:

```bash
docker logs edge-batdetect-service-1 --since 24h \
  | grep -oP 'validator:[^(]+' | sort | uniq -c | sort -rn
```
