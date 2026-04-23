"""Labeled spectrogram renderer for bat-call WAVs.

Renders a PNG with:
  * Time (s) on the x-axis
  * Frequency (kHz) on the y-axis, 0–max_freq_khz linear
  * Magnitude colour-mapped (viridis — bioacoustics standard)
  * Red bounding-boxes overlaid on every kept detection, labelled with
    the classifier's predicted species + confidence

Image is sized for inline display in the dashboard (~1200 × 400 px at
100 dpi). Matplotlib's Agg backend keeps this headless-safe.

matplotlib is imported lazily inside the function body so the module
can be imported on machines that don't have it installed. The Cloud
Function adds ``matplotlib`` to its requirements; the Pi doesn't, and
that's fine as long as the Pi doesn't call ``generate_spectrogram``.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple

import numpy as np
from scipy.signal import spectrogram as _spectrogram


def generate_spectrogram(
    audio: np.ndarray,
    sr: int,
    detection_pairs: Iterable[Tuple[dict, dict]],
    out_path: str,
    *,
    nperseg_sec: float = 0.004,
    overlap: float = 0.75,
    max_freq_khz: float = 140.0,
    figsize: Tuple[float, float] = (12.0, 4.0),
    dpi: int = 100,
    title: Optional[str] = None,
    with_boxes: bool = True,
) -> None:
    """Render a labelled spectrogram to ``out_path`` (PNG).

    Parameters
    ----------
    audio : 1-D waveform after any pipeline preprocessing (HPF already
        applied by the caller if that's what you want to show).
    sr : sample rate of ``audio``.
    detection_pairs : iterable of ``(detection_dict, prediction_dict)``
        tuples — same shape ``bat_pipeline.run_full_pipeline`` returns.
        Bounding boxes + labels are drawn from each pair.
    out_path : where to write the PNG.
    nperseg_sec, overlap : STFT window config. 4 ms / 75 % overlap is a
        reasonable compromise between time and frequency resolution in
        the bat band.
    max_freq_khz : upper y-axis cutoff. 140 kHz comfortably covers every
        NA species we detect and trims the high-Nyquist empty region.
    """
    # Deferred so the module imports without matplotlib on the Pi.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        raise ValueError("empty audio array — nothing to plot")

    nperseg = max(int(nperseg_sec * sr), 64)
    noverlap = int(overlap * nperseg)
    freqs, times, Sxx = _spectrogram(
        audio, fs=sr, window="hann",
        nperseg=nperseg, noverlap=noverlap,
        scaling="density", mode="magnitude",
    )

    # Log-magnitude looks dramatically better than linear. Clip to avoid
    # log(0) on silent frames.
    with np.errstate(divide="ignore"):
        Sxx_db = 20.0 * np.log10(np.maximum(Sxx, 1e-12))
    # Dynamic range clamp — show the top 80 dB of signal so bat calls
    # stay bright but background isn't a meaningless dark sheet.
    vmax = float(np.percentile(Sxx_db, 99))
    vmin = vmax - 80.0

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    freqs_khz = freqs / 1000.0
    pcm = ax.pcolormesh(
        times, freqs_khz, Sxx_db,
        cmap="viridis", vmin=vmin, vmax=vmax, shading="auto",
    )
    ax.set_ylim(0, max_freq_khz)
    ax.set_xlim(0, float(times[-1]) if times.size else float(len(audio)) / sr)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (kHz)")
    if title:
        ax.set_title(title, fontsize=10)

    cbar = fig.colorbar(pcm, ax=ax, pad=0.01)
    cbar.set_label("Magnitude (dB)", fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    # Overlay detection boxes only when ``with_boxes=True``. We now
    # render BOTH a clean version (no boxes) and an annotated one so the
    # dashboard can toggle them; see functions/main.py. Text labels are
    # intentionally omitted either way — in dense passes (20+ calls in
    # 15 s) overlapping labels turn the spec into unreadable graffiti,
    # and the dashboard detection list below the spec already carries
    # species + confidence metadata in 1:1 time order with the boxes.
    if with_boxes:
        for det, _pred in detection_pairs:
            start = float(det.get("start_time", 0.0))
            end = float(det.get("end_time", start))
            lo_khz = float(det.get("low_freq", 0.0)) / 1000.0
            hi_khz = float(det.get("high_freq", 0.0)) / 1000.0
            if end <= start or hi_khz <= lo_khz:
                continue
            rect = Rectangle(
                (start, lo_khz), end - start, hi_khz - lo_khz,
                linewidth=1.2, edgecolor="#ff3b3b", facecolor="none", alpha=0.95,
            )
            ax.add_patch(rect)

    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
