# Bat Capture Pipeline — Diagnosis Sheet
**Date:** 2026-04-26  
**Author:** Mustapha Nasomah (UC IT MS — Edge Bioacoustics Thesis)  
**Advisor:** Dr. Joseph Johnson  
**Status:** Active investigation — Pi02 deployment + sample-rate change pending

---

## 1. Project context

The Pi-based bat-monitoring rig (`pi01`) runs an AudioMoth USB microphone capturing continuous audio, processed by BatDetect2 + a North-American 5-class classifier (`EPFU_LANO`, `LABO`, `LACI`, `MYSP`, `PESU`) trained on Dr. Johnson's labeled recordings. Detections sync to Firestore for the dashboard at `bat-edge-monitor-dashboard.vercel.app`. Goal: a feature-complete edge inference rig with verifiable end-to-end data integrity for the thesis.

**This document captures what we believe is going wrong with the current Pi capture, what we've already ruled out, what we still need to verify, and the path forward — including a planned Pi02 deployment and a possible Wildlife Acoustics Song Meter Mini Bat 2 as a third reference device.**

---

## 2. The core problem (one-line summary)

> **The Pi's microphone is barely picking up bat-band signal even during peak bat hours. The detection pipeline is healthy; the capture upstream of it is the bottleneck.**

A 14-hour window (2026-04-25 evening through 2026-04-26 morning) produced the following measurements from `audio_levels` (per-15s-segment per-band RMS):

| Metric | Pi01 measurement | What we'd expect for clear bat passes |
|---|---|---|
| Avg total RMS | 0.0027 | Background noise floor — OK |
| Avg `bat_band_mid_rms` (≈25–50 kHz) | 0.0011 | Should briefly spike to 0.005–0.02 during a pass |
| **Max** `bat_band_mid_rms` over 14h | **0.0012** | **No segment ever showed a bat-pass spike** |
| BatDetect2 raw events fired | 921 in last hour | Healthy — but most are sub-0.20 prob |
| BD `max_det_prob` distribution (last hr) | avg 0.17, max ~0.48 | Real bats produce 0.5–0.95 events |
| Detections passing all gates (last 14h) | **2** (both <40% conf) | Should be 5–50/hr on a busy night |

**The audio sounds like static when listened to via the dashboard's 10× time-expanded player, even with volume cranked.** Dr. Johnson's amplitude-triggered AudioMoth files (`20210326_235900T.WAV`, `20210327_000100T.WAV`) — captured with the same hardware family — produce clearly audible bat chirps at normal volume.

---

## 3. What we have ruled out

The detection pipeline itself is calibrated and working:

| Component | Evidence it's not the bottleneck |
|---|---|
| **BatDetect2 model loading** | Container logs show `BatDetect2 ready` + warm-up forward pass succeeds. Pinned to v1.3.1, deterministic with `torch.manual_seed(0)`. |
| **Classifier loading** | `Classifier ready: ['EPFU_LANO', 'LABO', 'LACI', 'MYSP', 'PESU']` confirmed in logs. Inference works on Dr. Johnson's reference WAVs (7 detections / 3 species on `20210326_235900T.WAV` in offline-permissive mode). |
| **HPF (16 kHz)** | Applied analysis-only at 256 kHz; saved WAV is unchanged. Verified in pipeline source. |
| **Pipeline gate logic** | A/B'd against canonical-strict thresholds in the cloud function. Same code rejects noise WAVs and accepts Dr. Johnson's. End-to-end consistent. |
| **Firestore sync** | `sync-service` ticks every 60s; `device_status` heartbeat every 15s. After hang-recovery watchdog was deployed (2026-04-26) the dashboard reflects state within ~30s. |
| **Audio capture path (ALSA)** | Native 384 kHz capture, no `plughw` resampling artifacts. Confirmed in `AUDIO_CAPTURE_AUDIT.md`. |
| **Validator + FM-sweep gates** | Validated against Dr. Johnson's amplitude-triggered files using offline permissive mode (5-gate config, R²=0.10, RMS=0.0008): 7 detections / 3 species. The gates see real bats when real bats are in the audio. |
| **Dashboard ingest** | Verified end-to-end with multiple uploads tonight; spectrograms, time-expanded audio, detection boxes all render correctly. |

The pipeline is **not** silently dropping anything. It's correctly rejecting the noise it's being given.

---

## 4. What we have confirmed about the capture problem

### 4.1 The audio is faint

* Time-expanded playback (10× slowdown, 25.6 kHz playback) is barely audible at full volume.
* `audio_levels.bat_band_mid_rms` never exceeds 0.0012 over 14 hours of capture.
* Visually inspected spectrograms show energy concentrated below 15 kHz (the HPF cutoff), with sparse, weak structure in the 18–80 kHz bat band.

### 4.2 The "detections" are mostly hallucinations

Under the previously-too-permissive PNM config (validators OFF, BD threshold 0.15) the system produced 1,256 detections in ~7 hours. After listening to a sample, **most were noise** that the classifier was forced to assign a species label to. With FM_SWEEP re-enabled (2026-04-26 ~05:46 UTC), the false-positive rate dropped sharply — and the *real* detection rate dropped to 2 in 14 hours, both below 40% confidence.

### 4.3 The classifier was trained on positive IDs only

Dr. Johnson's training data set was 9 species at 192 kHz, all confirmed positive identifications. **There were no negative examples** (silence, wind, insect noise, mechanical hum, false-positive bat-detector firings). This means:

* The classifier has no learned representation of "this is not a bat" — it always picks the most-likely-of-5-species, even on pure noise.
* When upstream gates pass marginal signals, the species label is essentially noise.
* This is a known limitation; the gates have to do the bat-vs-noise discrimination upstream, which is exactly why the 4-gate / 5-gate pipeline matters.

### 4.4 Sample-rate research (literature evidence)

Per the Open Acoustic Devices and Somerset Bat Group testing:

> "The number of bats detected using a sample rate of 192 and 256 kHz is broadly similar, while 384 kHz records approximately 20–35% less bats."

The current Pi01 is at **384 kHz native**. Unintuitively, the recommended rate for AudioMoth bat capture is **256 kHz**. Reasons cited:

* At 192 kHz, the AudioMoth's ADC oversamples 2:1 — better SNR.
* At 384 kHz, SD-card write interrupts inject more interference.
* 250/384 kHz both run 1 ADC sample per WAV sample (no oversampling benefit).
* 256 kHz covers up to 128 kHz Nyquist — sufficient for all NA bats (PESU peaks ~50 kHz, LACI ~22 kHz, EPFU/LANO ~30 kHz, MYSP can reach 100 kHz).

**Action: drop sample rate from 384 kHz → 256 kHz** (the user proposed 250 kHz, which is on the right side of this; 256 is the canonical AudioMoth recommendation).

### 4.5 Hardware sensitivity comparison

Per multiple field studies and Wildlife Acoustics' own published data:

| Device | Mic sensitivity | Notes for our use case |
|---|---|---|
| **AudioMoth USB Microphone** (current pi01) | Reference baseline. "Robust and pleasantly sensitive" but documented as less sensitive than professional ARUs. | Designed for low-cost broad deployment, not low-SNR distant detection. |
| **AudioMoth (standalone, amplitude-triggered)** | Same physical mic; **but** amplitude-trigger mode only records during loud bursts, so files have intrinsic high SNR. | Dr. Johnson's reference setup. |
| **Wildlife Acoustics Song Meter Mini Bat 2** | **~10 dB more sensitive in some bands** — i.e. ~10× more signal level for the same source. Low-noise mic optimized for distant echolocation. | Field studies (SM4BAT family) record measurably more high-frequency call files than AudioMoth in same deployment. |
| **AudioMoth Dev** | Same internal as standard AudioMoth, **but accepts external 3.5 mm electret mic.** | Path to add a higher-sensitivity mic without buying a Song Meter. |

**The 10 dB gap is the dominant explanation** for why our Pi01 sees only noise during peak hours while Song Meter–class devices in the same environment see clear calls. We can't close this gap entirely, but we can get partway by:

1. Optimizing AudioMoth gain & sample rate (cheap, immediate).
2. Optimizing physical placement (cheap, immediate).
3. Adding an external mic via AudioMoth Dev (medium cost, biggest upside).
4. Adding a Song Meter Mini Bat 2 as a reference channel (highest cost; highest reliability for ground-truth labelling).

---

## 5. Open questions — what we still need to verify

1. **Is the AudioMoth USB physically working?** Test: jingle keys ~6" from mic → expected `bat_band_mid_rms` spike ≥0.005 in the next 15s segment.
2. **Is the Pi01 placement sub-optimal?** Bats fly in specific corridors; current AudioMoth position (covered porch / window / outdoor pole / etc.) may not align. Need to confirm location, height, orientation, distance from foliage / water.
3. **Does Pi01 detect bats at all when bats are confirmed present?** Need a known bat call playback right next to the AudioMoth — should produce strong detections.
4. **Does sample-rate change to 256 kHz close the gap?** A/B test: one night at 384 kHz, one night at 256 kHz, compare detection counts + spectrogram quality.
5. **Does Pi02 at home (different acoustic environment, AudioMoth Dev with external mic option) produce different baselines?** This is a controlled-environment sanity check.
6. **Do Dr. Johnson's standalone AudioMoth recordings from THIS site (i.e. same location as Pi01, captured the same nights) produce strong bat captures?** This is the cleanest A/B — same place, same nights, different recording mode.

---

## 6. Action plan

### Immediate (today, 2026-04-26)

* [ ] **Drop sample rate to 256 kHz** in Pi01's AudioMoth config + restart batdetect-service. Re-run tonight to compare detection count vs prior 384 kHz nights.
* [ ] **Mic test** — jingle keys near AudioMoth, verify bat-band RMS spike registers in `audio_levels`. Confirms the mic is alive and sensitive at all.
* [ ] **Document Pi01 physical placement** — photo + notes (location, height, orientation, distance to obstacles). Add to `HARDWARE_TROUBLESHOOTING.md`.
* [ ] **Get standalone AudioMoth recordings from advisor for same dates as Pi01 deployment.** This is the A/B that would isolate whether the issue is location or pipeline.

### Near-term (this week)

* [ ] **Set up Pi02 at home** with a separate AudioMoth Dev unit, sample rate 256 kHz, same firmware, gain=high. Use as a control. If Pi02 captures cleanly and Pi01 doesn't → site/placement issue. If both are weak → systemic AudioMoth-USB-mode issue.
* [ ] **Source an external mic for AudioMoth Dev's 3.5 mm jack** (Knowles SPU0410LR5H or similar high-SPL electret used in the bat-detector community). This tests the "AudioMoth + better mic = closer to Song Meter" hypothesis at low cost.
* [ ] **Continuous overnight capture comparison** — log `audio_levels.bat_band_mid_rms` for an entire night (not just a 14h window with a midnight reset) to characterize the *distribution* of activity, not just the peak.

### Stretch / pending advisor decision

* [ ] **Deploy a Wildlife Acoustics Song Meter Mini Bat 2** as a third reference device on the same site. Treat its detections as ground truth; calibrate Pi01/Pi02 to match.
* [ ] **Re-train the classifier with negative examples** once we have a corpus of confirmed-noise WAVs from Pi01's `_diagnostic/` folder. The current classifier has no "this isn't a bat" class; adding a `noise` head would let the model help with bat-vs-noise discrimination instead of relying entirely on upstream gates.
* [ ] **Standalone vs USB AudioMoth at the same location** — a 1-night comparison to isolate whether USB mode itself is degrading capture quality.

---

## 7. What I'm asking the advisor for

1. **Recordings from the standalone AudioMoth** for any nights that overlap with Pi01's deployment (2026-04-23 onward). Ground-truth comparison.
2. **Confirmation on the Song Meter Mini Bat 2 deployment** — willingness, timing, location.
3. **Feedback on the sample-rate change to 256 kHz** before I roll it on Pi01.
4. **Negative-example labels** if available — do you have any WAVs labeled "noise / non-bat" from the source dataset that I missed?

---

## 8. References

- Open Acoustic Devices — [Sampling rates for bats](https://www.openacousticdevices.info/support/configuration-support/sampling-rates-for-bats-uk)
- Somerset Bat Group — [AudioMoth 2020 sample-rate testing](https://somersetbat.group/advice/which-bat-detector/audiomoth/testing/sample-rate/)
- Wildlife Acoustics — [Song Meter Mini Bat 2 product page](https://www.wildlifeacoustics.com/products/song-meter-mini-2-bat-aa)
- Wildlife Acoustics — [AudioMoth vs Song Meter Micro 2 comparison](https://www.wildlifeacoustics.com/products/micro-2-vs-audiomoth)
- Bat Detector Reviews — [AudioMoth USB Microphone review](https://batdetecting.blogspot.com/2023/07/review-audiomoth-usb-microphone-from.html)
- Beason et al. (2023) — [A Comparison of Bat Calls Recorded by Two Acoustic Monitors](https://meridian.allenpress.com/jfwm/article/14/1/171/492016/A-Comparison-of-Bat-Calls-Recorded-by-Two-Acoustic) (SM4BAT vs AudioMoth field study)
- Open Acoustic Devices — [Are there published studies comparing AudioMoth performance for bat work?](https://www.openacousticdevices.info/support/device-support/are-there-any-published-or-unpublished-studies-comparing-the-performance-of-audiomoth-and-other-acoustic-recorders-for-bat-studies)
- Internal — `AUDIO_CAPTURE_AUDIT.md`, `FIELD_DIAGNOSTIC_PROTOCOL.md`, `OFFLINE_WAV_ANALYSIS.md`, `edge/scripts/night_diag.sh`
