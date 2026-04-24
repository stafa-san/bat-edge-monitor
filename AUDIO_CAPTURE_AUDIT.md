# Live Audio Capture — Deep Audit (early-pipeline stages)

**Date:** 2026-04-23
**Scope:** microphone → USB → ALSA → arecord → WAV → `bat_api.load_audio` → HPF
(i.e. *before* BatDetect2 sees anything)
**Prompted by:** "what could be holding back live bat detection" — after the
earlier `PIPELINE_AUDIT_AND_FIXES.md` and `BATDETECT2_STABILITY_FIX.md` work.

---

## TL;DR

Eight stages audited end-to-end. **One real, fixable issue found**: the ALSA
`plughw` plugin is using its built-in **linear resampler** to downsample the
AudioMoth's native 384 kHz stream to the 256 kHz we request. Linear resampling
has no proper anti-alias filter, so any energy between 128 kHz and 192 kHz
(above the new Nyquist) folds back into the bat band as aliased noise. Fix is
one-line: capture at the AudioMoth's native 384 kHz and let BatDetect2's own
scipy polyphase resampler do the one downsample to 256 kHz. Shipped in this
commit.

Everything else is healthy:
- USB transport clean, zero errors / xruns / overruns since boot.
- HPF roll-off at 16 kHz is benign — -2.8 dB at 18 kHz (LACI's lower tail), essentially flat above 22 kHz.
- Pi 5 at 372 % / 4 cores during live capture — under saturation, thermal
  57 °C, no throttling. Producer/consumer refactor is actually parallelising.
- BatDetect2 model config + resize_factor are as designed.

**Still outside software visibility — must confirm with advisor**:
- AudioMoth gain-preset setting (low / med-low / med / med-high / high / very-high)
- AudioMoth hardware HPF setting (off / 1 kHz / 8 kHz / 16 kHz)

---

## Stage A — AudioMoth USB descriptors

Probed with `lsusb -v -d 16d0:06f3`:

```
iProduct      384kHz AudioMoth USB Microphone
bInterfaceClass  1 Audio  / Streaming
bNrChannels      1
bBitResolution   16
bSamFreqType     1 Discrete           ← IMPORTANT
tSamFreq[ 0]     384000                ← device only offers 384 kHz
wMaxPacketSize   0x0300  1x 768 bytes  ← high-speed USB
```

**Finding**: the AudioMoth only advertises **one sample rate — 384 000 Hz**
(`bSamFreqType=1 Discrete`). There is no way to configure it to stream at
256 kHz natively. If we ask ALSA for 256 kHz, the kernel / plugin layer has to
resample.

---

## Stage B — ALSA hw-params (what's actually happening in the stream)

From `/proc/asound/card2/pcm0c/sub0/hw_params` while `batdetect-service` is
actively capturing:

```
format:       S16_LE
channels:     1
rate:         384000 (384000/1)         ← HARDWARE rate (native)
period_size:  48000      ≈ 125 ms
buffer_size:  192002     ≈ 500 ms
```

And from `/proc/asound/card2/stream0`:
```
Capture:
  Momentary freq = 384000 Hz
  Interface 1  Altset 1
  Format: S16_LE  Channels: 1
  Rates: 384000
```

So at the **kernel/USB level we're capturing at 384 kHz**. Our container asks
for 256 kHz via `arecord -r 256000 -D plughw:2,0`; plughw transparently
opens the device at 384 kHz and resamples down to 256 kHz before giving
samples to arecord. The WAV file on disk is 256 kHz.

### Which resampler is plughw using?

- No `/etc/asound.conf` on the Pi.
- No `libasound_module_rate_*` packages installed (`ls /usr/lib/aarch64-linux-gnu/alsa-lib/` returned nothing for rate plugins).
- ALSA's default fallback when no `speexrate` / `samplerate` plugin is available is the built-in **linear interpolator**.

### Why this matters

Linear interpolation does **not** apply an anti-alias lowpass filter before
decimation. So when ALSA decimates 384 000 Hz → 256 000 Hz, energy above the
new Nyquist (128 kHz) aliases back into the band we care about:

```
Original Nyquist: 192 kHz
New Nyquist:      128 kHz
Alias zone:       128–192 kHz folds to 64–128 kHz
```

That's the upper band of the bat spectrum (60–128 kHz — where Myotis and
some Pipistrellus calls live). Any ultrasound in 128–192 kHz (high-frequency
insect clicks, electrical interference, high-pitched mechanical resonance)
gets mirrored back into the band the detector analyses, polluting the signal.

### The fix we're shipping

Change `SAMPLE_RATE=256000` → `SAMPLE_RATE=384000` in
[`edge/docker-compose.yml`](edge/docker-compose.yml). Chain:

1. arecord captures natively at 384 kHz — **no resampling in the kernel**.
2. WAV file written at 384 kHz (file is ~50 % larger; storage is not the bottleneck).
3. `bat_api.load_audio(path)` reads the 384 kHz WAV and resamples to BatDetect2's `target_samp_rate=256000` using scipy's polyphase implementation, which includes a proper anti-alias filter.
4. HPF and everything downstream unchanged (HPF is designed against the BD target rate of 256 kHz).

Net: **one resample instead of one bad resample, using scipy's high-quality polyphase filter instead of ALSA's linear interp.**

---

## Stage C — USB transport errors

```
dmesg | grep -E "AudioMoth|xrun|overrun|underrun|audio.*err|snd_usb"
```

Result: only the boot-time enumeration line (`usb 1-2: Product: 384kHz
AudioMoth USB Microphone`). **Zero errors, zero xruns, zero buffer
overruns/underruns**. USB transport is clean.

---

## Stage D — ALSA buffer state (is the stream steady?)

From `/proc/asound/card2/pcm0c/sub0/status` during active capture:

```
state:        RUNNING
avail:        14976     (samples currently in buffer)
avail_max:    49536     (peak buffer fullness observed)
buffer_size:  192002
```

Peak fullness ≈ 26 % of the buffer. Not under pressure; plenty of headroom
before overrun risk. Stream is stable.

---

## Stage E — CPU/memory/thermal during active capture

```
docker stats --no-stream batdetect-service  →  CPU 372.97 %   (out of 400 %)
vcgencmd measure_temp                        →  57.1 °C
vcgencmd get_throttled                       →  0x0
cat /proc/loadavg                             →  3.40 1.84 0.76
```

At 372 % of 400 cores-available, the Pi 5 is running near the wall. This is
actually **evidence the producer/consumer refactor is working**: inference
(`torch.set_num_threads(4)`) and capture (async arecord subprocess) are
overlapping, saturating available cores.

No thermal throttling, plenty of thermal headroom (80 °C is the throttle
point). Load-average 3.40 / 4 cores = 85 % utilisation, matches CPU%.

**Caveat**: this is tight. If processing ever runs slower than capture
(thermal throttling, GC spike, I/O spike), the queue fills and the producer
blocks → we'd start losing duty cycle again. Watch for `[BAT] MODEL-HEALTH
WARNING` in the daily summary.

---

## Stage F — HPF response at the LACI low-harmonic band

Current config: `HPF_CUTOFF_HZ=16000`, `HPF_ORDER=4`, applied via
`sosfiltfilt` (zero-phase = filter runs forward **and** backward, so effective
response is **8th-order**, and dB numbers double).

Computed in the running container:

| Freq (kHz) | Gain (dB) | Notes |
|---|---|---|
| 12 | −21.13 | below bat band — kill |
| 15 | −8.65  | LACI absolute floor |
| **16** | **−6.04** | cutoff (doubled by sosfiltfilt) |
| 17 | −4.13 | LACI lower tail |
| **18** | **−2.79** | LACI typical low |
| 20 | −1.27 | low end of most calls |
| 22 | −0.59 | LACI peak freq |
| 25 | −0.21 | EPFU / LANO low |
| 30 | −0.04 | broad bat band — flat |
| 40 |  0.00 | LABO / LACI main — flat |

Effective −N dB points:
- −0.5 dB at 22.5 kHz
- −1.0 dB at 20.6 kHz
- −3.0 dB at 17.8 kHz

**Verdict**: benign. LACI calls typically peak 22–25 kHz (<0.6 dB loss) with
lower harmonics reaching 18 kHz (−2.8 dB loss). Other groups (EPFU, LANO,
LABO, MYSP, PESU) sit comfortably above 25 kHz where attenuation is
essentially zero. A 2–3 dB attenuation on the LACI low-tail is acceptable
— the main call energy is still passed with negligible loss.

If we ever want to preserve LACI lower harmonics better, options are to
drop `HPF_CUTOFF_HZ` to 12 kHz (−1.2 dB at 18 kHz, −0.3 dB at 22 kHz) but
this lets more mechanical / fan rumble through into the detector. Not
recommending a change right now — the band where real bat-call energy
concentrates is untouched.

---

## Stage G — BatDetect2 `resize_factor=0.5`

From `bat_api.get_config()` inside the running container:

```
fft_win_length      0.002          (2 ms)
fft_overlap         0.75
spec_height         256
max_freq            120000
min_freq            10000
resize_factor       0.5            ← halves spectrogram resolution
```

BatDetect2 generates its spectrogram at full resolution then resizes by 0.5
(both axes) before passing to the CNN. Post-resize:

- Spectrogram height: 256 → **128 bins** covering 10 kHz to 120 kHz → **860 Hz/bin**
- Time resolution: ~0.5 ms hop → 1.0 ms hop after resize

This is intentional in BatDetect2's design — the model was trained on
half-resolution specs and the tradeoff is chosen for inference speed vs.
accuracy. Changing it would mean retraining the model.

**Verdict**: not a source of missed detections for us — the network has seen
this resolution during training. Don't touch it. (If we ever retrain for NA,
we can consider resize_factor=1.0 at training time; notebook in
`RETRAINED_NA_DETECTOR_PLAN.md`.)

---

## Stage H — What's not visible from software (needs advisor)

Two AudioMoth settings are configured via the macOS AudioMoth app **before**
plugging into the Pi. In USB-mic mode there's no way to read them back from
the host; `lsusb` shows only the USB audio class descriptor, not the device's
internal audio chain state.

### H.1 Gain preset

AudioMoth firmware 1.3.1 exposes six input-gain presets (button cycles):

| Preset | Gain | Intended use |
|---|---|---|
| low      | ~15 dB | very-close passes, loud calls |
| med-low  | ~20 dB | typical close passes |
| med      | ~30 dB | general-purpose (Dr. Johnson's default?) |
| med-high | ~35 dB | open-field surveys |
| high     | ~40 dB | distant passes |
| very-high | ~45 dB | maximum range, noise at quiet sites |

**Why it matters**: the Pi has been running at p50 RMS ~0.0025 with peaks up
to 0.9 on known-loud sources. That suggests the gain is **correctly set** —
the mic responds to loud input (peaks) without being permanently saturated
(low baseline). If real field bat passes on April 21 evening logged RMS
0.015–0.02 with `bd_raw_count` up to 66, the gain setting was compatible
with those conditions.

**Action**: confirm with Dr. Johnson whether the gain was deliberately set,
or if it's whatever default the AudioMoth shipped with. Not blocking
detections as-is.

### H.2 Hardware HPF

AudioMoth firmware 1.3.1 exposes four hardware HPF settings: OFF / 1 kHz /
8 kHz / 16 kHz. We cannot read this from USB-mic mode.

Our software HPF at 16 kHz sits **after** whatever the AudioMoth already
did — it's additive. If the AudioMoth is set to 16 kHz hardware HPF AND we
apply a 16 kHz software HPF, the compound response at 18 kHz is roughly
−5 to −6 dB instead of −2.8 dB — starting to trim LACI low-tail more than
we want.

**Action**: confirm AudioMoth HPF is either OFF or 8 kHz. If it's 16 kHz,
we can drop our software HPF cutoff to 12 kHz to compensate. Once again,
not blocking; a second-order optimisation.

---

## Summary of recommendations

| # | Finding | Action | Blast radius |
|---|---|---|---|
| 1 | ALSA linear-resampler aliasing | `SAMPLE_RATE=384000` → native capture | one env var |
| 2 | HPF −2.8 dB at 18 kHz | Leave alone; benign | none |
| 3 | AudioMoth gain preset | Confirm with advisor | info-gathering |
| 4 | AudioMoth hardware HPF | Confirm with advisor | info-gathering |
| 5 | Pi 5 at 93 % CPU during inference | Monitor; no action yet | observability |
| 6 | Everything else | Healthy | — |

Item #1 ships in this commit. Items #3 and #4 are notes for your next
conversation with Dr. Johnson.

---

## Verification after native-rate switch

After the next `docker compose up -d batdetect-service`:

```bash
# Confirm actual capture rate on the kernel side
cat /proc/asound/card2/pcm0c/sub0/hw_params | grep rate
# Expected: rate: 384000 (384000/1) — unchanged

# Confirm WAV file on disk is 384 kHz now
docker compose exec -T batdetect-service bash -c '
  f=$(ls -t /tmp/tmp*/bat_audio.wav 2>/dev/null | head -1)
  [ -n "$f" ] && file "$f"
'
# Expected: ... WAVE audio ... 16 bit, mono 384000 Hz
```

And `audio_levels` table should continue populating at ~15 s cadence (the
producer/consumer refactor is orthogonal to the sample-rate change — capture
loop structure is unchanged).
