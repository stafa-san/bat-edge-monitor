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
