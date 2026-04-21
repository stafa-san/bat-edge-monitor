"""Audio-level bat-call sanity checks.

A third gate after the BatDetect2 base threshold and the NA-groups
classifier. Its job is the one the classifier cannot do: say "this
isn't a bat call at all." The classifier's softmax always picks one of
five groups — when fed broadband fan noise it happily returns a
confident LACI because mechanical low-frequency noise is, by
coincidence, the closest thing in feature space to a Hoary Bat call.

These checks run directly on the in-memory audio array (already
resampled to BatDetect2's target rate) and cost a few hundred
milliseconds per 15-second segment. No ML, no model — just three
cheap signal-processing assertions about what a real echolocation
pulse looks like:

1. **RMS floor** — the segment has to be audibly louder than silence.
   Rejects the "nothing was really there" case that inflated our
   tier-1 archive with 0.0012-RMS files.

2. **Bat-band SNR** — peak energy in 15-120 kHz vs median in the same
   band. A real pulse is a concentrated bright spot; broadband noise
   is uniform. If peak/median is small, it's noise.

3. **Temporal burst ratio** — real bat passes are short transients
   sitting in an otherwise quiet 15-second window. If the bat-band
   energy is the same in every frame (no burst), it's steady-state
   noise.

All three thresholds are tunable from the caller. Rejection returns a
short machine-parseable reason string so we can log why a segment was
dropped and tune later from real misses.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from scipy.signal import spectrogram


# Bat echolocation band. NA bats of interest fall comfortably inside
# this range; the LACI lower edge at ~18 kHz is the most aggressive
# call we're sensitive to. 120 kHz upper bound handles the small
# Myotis. Extended by half the Nyquist margin at 256 kHz.
BAT_BAND_LOW_HZ = 15000.0
BAT_BAND_HIGH_HZ = 120000.0


def _bat_band_spectrogram(
    audio: np.ndarray, sr: int,
    nperseg_sec: float = 0.002,
    overlap: float = 0.75,
) -> np.ndarray:
    """Magnitude spectrogram rows restricted to the bat band.

    2 ms windows with 75% overlap — same time resolution regime
    BatDetect2 uses internally, so we see the same kind of transient
    the detector would.
    """
    nperseg = max(int(nperseg_sec * sr), 32)
    noverlap = int(overlap * nperseg)
    freqs, _t, Sxx = spectrogram(
        audio, fs=sr, window="hann",
        nperseg=nperseg, noverlap=noverlap,
        scaling="density", mode="magnitude",
    )
    band = (freqs >= BAT_BAND_LOW_HZ) & (freqs <= min(sr / 2.0, BAT_BAND_HIGH_HZ))
    return Sxx[band, :]


def has_bat_call_shape(
    audio: np.ndarray,
    sr: int,
    start_time: float,
    end_time: float,
    min_slope_khz_per_ms: float = -0.1,
    max_low_band_ratio: float = 0.5,
    min_r2: float = 0.2,
    low_band_cutoff_hz: float = 15000.0,
    pad_ms: float = 10.0,
    frame_ms: float = 1.0,
    frame_overlap: float = 0.5,
) -> Tuple[bool, str, dict]:
    """Per-detection shape check — distinguishes real echolocation from
    broadband noise (insect clicks, mechanical contacts, rain drops).

    Runs on the local audio window around BatDetect2's detection bounding
    box. Checks THREE things that characterise real bat calls but NOT
    broadband noise:

    1. **Low-band-energy ratio.** A real bat call concentrates its energy
       in the ultrasonic band (≥ 15 kHz). Broadband clicks dump energy
       across the full spectrum including sub-bat frequencies. If the
       0-15 kHz power inside the call window is > 50% of the 15+ kHz
       power, it's not a bat — it's a click.

    2. **Coherent peak frequencies (R²).** A real bat call's per-frame
       peak frequency follows a smooth line. A broadband click has
       peaks jumping chaotically across the bat band, so R² of the
       linear fit is near zero. R² > 0.2 required.

    3. **Downward FM sweep slope.** Bat echolocation pulses sweep
       downward in frequency (EPFU ~40 → 25 kHz over 10 ms, LACI
       ~28 → 22 kHz over 15 ms, etc). Slope must be more negative
       than -0.1 kHz/ms.

    All three must pass. Any failure returns a specific reason string.
    Returns ``(is_bat_call, reason, stats)``. ``stats`` always carries
    the measured numbers so the caller can log them for tuning.
    """
    stats = {
        "slope_khz_per_ms": None,
        "fit_r2": None,
        "low_band_ratio": None,
        "n_frames_used": 0,
    }

    if audio is None or audio.size == 0:
        return False, "empty_audio", stats
    if end_time <= start_time:
        return False, "invalid_bounds", stats
    # Guard against non-finite values (NaN/Inf) — scipy polyfit's
    # internal SVD raises LinAlgError on those, which would bubble
    # up as an uncaught exception in the main capture loop.
    if not np.isfinite(audio).all():
        return False, "non_finite_audio", stats

    # Extract a padded window around the detection's time bounding box.
    # Padding matters — BatDetect2 gives tight bounding boxes (~10 ms
    # call in a ~10 ms box), so we pad generously to have context for
    # the baseline and enough frames for a slope fit.
    pad = pad_ms / 1000.0
    lo = max(0, int((start_time - pad) * sr))
    hi = min(len(audio), int((end_time + pad) * sr))
    window = audio[lo:hi]
    min_samples = int(4 * frame_ms / 1000.0 * sr)
    if len(window) < min_samples:
        return False, f"window_too_short({len(window)}smp)", stats

    # Spectrogram. 1 ms frames give 1 kHz frequency resolution at
    # 256 kHz — fine enough to resolve LACI's ~0.4 kHz/ms sweep.
    nperseg = max(int(frame_ms / 1000.0 * sr), 64)
    noverlap = int(frame_overlap * nperseg)
    freqs, times, Sxx = spectrogram(
        window.astype(np.float32), fs=sr, window="hann",
        nperseg=nperseg, noverlap=noverlap, mode="magnitude",
    )

    # ---- Low-band energy ratio -------------------------------------------
    # Measured AT THE PEAK FRAME (loudest moment), not averaged over the
    # whole window. Averaging across mostly-ambient padding makes both
    # bands look like "ambient everywhere" and loses the call's signature.
    # At the peak frame:
    #   - Real bat call: bat-band peak is the call (loud), low-band peak
    #     is ambient (quiet). Ratio ≈ 0.01.
    #   - Broadband click: bat-band peak and low-band peak are both from
    #     the same click event. Ratio ≈ 1.0.
    low_band = freqs < low_band_cutoff_hz
    bat_band = freqs >= low_band_cutoff_hz
    if not low_band.any() or not bat_band.any():
        return False, "bad_frequency_split", stats

    # Frame with the loudest bat-band energy — anchors the measurement
    bat_max_per_frame = np.max(Sxx[bat_band, :], axis=0)
    if bat_max_per_frame.size == 0 or float(bat_max_per_frame.max()) <= 0:
        return False, "bat_band_silent", stats
    peak_frame = int(np.argmax(bat_max_per_frame))
    # ±2 frame window around the peak (5 frames = ~5 ms at 1 ms frames)
    f0 = max(0, peak_frame - 2)
    f1 = min(Sxx.shape[1], peak_frame + 3)

    peak_low = float(np.max(Sxx[low_band, f0:f1]))
    peak_bat = float(np.max(Sxx[bat_band, f0:f1]))
    if peak_bat <= 0:
        return False, "bat_band_silent", stats

    low_band_ratio = peak_low / peak_bat
    stats["low_band_ratio"] = round(low_band_ratio, 3)

    if low_band_ratio > max_low_band_ratio:
        return False, f"broadband_noise(lowband_ratio={low_band_ratio:.2f})", stats

    # ---- FM sweep with WEIGHTED LS (frames with more bat-band energy
    #      dominate the fit). Unweighted polyfit on a window containing
    #      mostly ambient padding was getting dragged around by the
    #      padding's random peak frequencies. Weighting makes the call
    #      drive the fit while ambient frames contribute ~nothing.
    bat_Sxx = Sxx[bat_band, :]
    bat_freqs = freqs[bat_band]
    if bat_Sxx.size == 0 or bat_Sxx.shape[1] < 4:
        return False, "too_few_frames", stats

    peak_freqs = bat_freqs[np.argmax(bat_Sxx, axis=0)]
    # Weight each frame by its bat-band peak energy (normalised).
    peak_per_frame = bat_max_per_frame  # reused from above
    w = peak_per_frame / (float(peak_per_frame.max()) + 1e-20)
    stats["n_frames_used"] = int(len(times))

    # polyfit accepts per-point weights; these are sqrt(weight) internally.
    slope_hz_per_s, intercept = np.polyfit(times, peak_freqs, 1, w=w)
    slope_khz_per_ms = float(slope_hz_per_s) / 1_000_000.0
    stats["slope_khz_per_ms"] = round(slope_khz_per_ms, 3)

    # R² on only the active frames (weight ≥ 0.3 of peak). That's the
    # frames where the call actually lives; scoring the fit against the
    # padding frames would be misleading.
    active = w >= 0.3
    if active.sum() < 4:
        return False, "too_few_active_frames", stats
    t_a = times[active]
    f_a = peak_freqs[active]
    predicted = slope_hz_per_s * t_a + intercept
    ss_res = float(np.sum((f_a - predicted) ** 2))
    ss_tot = float(np.sum((f_a - f_a.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    stats["fit_r2"] = round(r2, 3)

    if r2 < min_r2:
        return False, f"chaotic_peaks(r2={r2:.2f})", stats

    # Real bat calls sweep down. min_slope_khz_per_ms is negative;
    # the measured slope must be more negative than that threshold.
    if slope_khz_per_ms > min_slope_khz_per_ms:
        return False, f"not_downward_sweep(slope={slope_khz_per_ms:+.2f}kHz/ms)", stats

    return True, "ok", stats


def is_likely_bat_call(
    audio: Optional[np.ndarray],
    sr: int,
    min_rms: float = 0.005,
    min_snr_db: float = 10.0,
    min_burst_ratio: float = 3.0,
) -> Tuple[bool, str]:
    """Return ``(is_bat, reason)``.

    When ``is_bat`` is False the ``reason`` string includes the failing
    metric's value so callers can log it verbatim and tune later.
    """
    if audio is None or audio.size == 0:
        return False, "empty_audio"
    if not np.isfinite(audio).all():
        return False, "non_finite_audio"

    audio_f = audio.astype(np.float32, copy=False)

    # ---- Test 1 — RMS floor (catches near-silent segments) ----
    rms = float(np.sqrt(np.mean(audio_f ** 2)))
    if rms < min_rms:
        return False, f"rms_too_low({rms:.4f})"

    # ---- Test 2 — peak-to-median SNR inside the bat band ----
    Sxx = _bat_band_spectrogram(audio_f, sr)
    if Sxx.size == 0:
        return False, "bat_band_empty"

    peak = float(np.max(Sxx))
    median = float(np.median(Sxx))
    if median <= 0 or peak <= 0:
        return False, "bat_band_degenerate"
    # Magnitude spectrogram → 20 log10
    snr_db = 20.0 * np.log10(peak / median)
    if snr_db < min_snr_db:
        return False, f"snr_too_low({snr_db:.1f}dB)"

    # ---- Test 3 — temporal burst (peak frame vs median frame) ----
    frame_peaks = np.max(Sxx, axis=0)
    top_frame = float(np.percentile(frame_peaks, 95))
    median_frame = float(np.median(frame_peaks))
    if median_frame <= 0 or top_frame <= 0:
        return False, "frame_stats_degenerate"
    burst = top_frame / median_frame
    if burst < min_burst_ratio:
        return False, f"no_burst({burst:.2f}x)"

    return True, "ok"
