"""Labeled spectrogram renderer for bat-call WAVs.

Two visual palettes are supported — pick at call time via the
``palette`` argument:

``viridis`` (default, perceptually uniform)
    The bioacoustics-neutral choice. Colourblind-friendly, equal
    magnitude steps per colour step, safe for figures that go into
    thesis publications. Matplotlib's built-in ``viridis`` colourmap.

``sonobat`` (high-contrast bat-research style)
    Matches the look that practising bat researchers are used to from
    Wildlife Acoustics SonoBat — dark navy background, bright cyan
    call peaks that "pop," fine time resolution so the FM sweep of
    each call is visible, a thin 5 kHz grid, and a waveform strip
    under the spectrogram. Custom colour map + tighter dynamic range
    + finer STFT window.

Regardless of palette, red detection boxes + species labels overlay
when ``with_boxes=True``. Image size is tuned for inline dashboard
display (~1200 × 400 px at 100 dpi).

matplotlib is imported lazily inside the function body so the module
can be imported on machines without it installed. The Cloud Function
lists ``matplotlib`` in its requirements; the Pi doesn't, which is
fine as long as the Pi never calls ``generate_spectrogram``.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple

import numpy as np
from scipy.signal import spectrogram as _spectrogram


# -----------------------------------------------------------------------------
# Per-palette visual settings. Tuned via side-by-side comparison against
# real AudioMoth field recordings + reference SonoBat output from
# Dr. Johnson's corpus.
# -----------------------------------------------------------------------------

_SONOBAT_STOPS = [
    (0.00, "#000a1a"),  # near-black, navy tint
    (0.22, "#0a2a5a"),  # dark blue
    (0.45, "#1c58a4"),  # medium blue
    (0.65, "#3a8cd6"),  # bright blue
    (0.80, "#66c7f0"),  # cyan
    (0.92, "#b2e8ff"),  # light cyan
    (1.00, "#f4fbff"),  # near-white highlight
]


def _palette_settings(palette: str) -> dict:
    """Return the STFT + display settings appropriate for the palette."""
    if palette == "sonobat":
        return {
            "nperseg_sec": 0.0015,   # 1.5 ms — fine time resolution so
                                     # each call's downward FM sweep is
                                     # visible as a distinct curved stripe
            "overlap": 0.90,         # very high overlap → smooth sweep curves
            "dynamic_range_db": 42,  # tight clip → calls glow, background
                                     # compresses to near-black
            "cmap_name": "sonobat",
            "grid": True,
            "waveform_strip": True,
        }
    # viridis default — what was already shipping
    return {
        "nperseg_sec": 0.004,
        "overlap": 0.75,
        "dynamic_range_db": 80,
        "cmap_name": "viridis",
        "grid": False,
        "waveform_strip": False,
    }


def generate_spectrogram(
    audio: np.ndarray,
    sr: int,
    detection_pairs: Iterable[Tuple[dict, dict]],
    out_path: str,
    *,
    max_freq_khz: float = 140.0,
    figsize: Tuple[float, float] = (12.0, 4.0),
    dpi: int = 100,
    title: Optional[str] = None,
    with_boxes: bool = True,
    palette: str = "viridis",
) -> None:
    """Render a labelled spectrogram to ``out_path`` (PNG).

    Parameters
    ----------
    audio : 1-D waveform after any pipeline preprocessing (HPF already
        applied by the caller if that's what you want to show).
    sr : sample rate of ``audio``.
    detection_pairs : iterable of ``(detection_dict, prediction_dict)``
        tuples — same shape ``bat_pipeline.run_full_pipeline`` returns.
    out_path : PNG destination.
    max_freq_khz : y-axis cap. 140 kHz covers every NA species and
        trims the mostly-empty high-Nyquist region.
    with_boxes : overlay red detection rectangles + species labels when
        True. Omitted otherwise (clean view).
    palette : "viridis" (default, perceptually uniform) or "sonobat"
        (high-contrast bat-research style with waveform strip + grid).
    """
    # Deferred so the module imports without matplotlib on the Pi.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import Rectangle

    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        raise ValueError("empty audio array — nothing to plot")

    settings = _palette_settings(palette)
    nperseg = max(int(settings["nperseg_sec"] * sr), 64)
    noverlap = int(settings["overlap"] * nperseg)

    freqs, times, Sxx = _spectrogram(
        audio, fs=sr, window="hann",
        nperseg=nperseg, noverlap=noverlap,
        scaling="density", mode="magnitude",
    )

    with np.errstate(divide="ignore"):
        Sxx_db = 20.0 * np.log10(np.maximum(Sxx, 1e-12))
    vmax = float(np.percentile(Sxx_db, 99))
    vmin = vmax - settings["dynamic_range_db"]

    # Pick or build the colormap.
    if settings["cmap_name"] == "sonobat":
        colors = [stop for _, stop in _SONOBAT_STOPS]
        positions = [pos for pos, _ in _SONOBAT_STOPS]
        cmap = LinearSegmentedColormap.from_list(
            "sonobat", list(zip(positions, colors)), N=256,
        )
    else:
        cmap = plt.get_cmap(settings["cmap_name"])

    # Layout: optionally a thin waveform strip underneath the spec.
    if settings["waveform_strip"]:
        fig, (ax_spec, ax_wave) = plt.subplots(
            2, 1,
            figsize=(figsize[0], figsize[1] + 0.9),
            dpi=dpi,
            gridspec_kw={"height_ratios": [5, 1], "hspace": 0.05},
            sharex=True,
        )
    else:
        fig, ax_spec = plt.subplots(figsize=figsize, dpi=dpi)
        ax_wave = None

    freqs_khz = freqs / 1000.0
    pcm = ax_spec.pcolormesh(
        times, freqs_khz, Sxx_db,
        cmap=cmap, vmin=vmin, vmax=vmax, shading="auto",
    )
    ax_spec.set_ylim(0, max_freq_khz)
    ax_spec.set_xlim(
        0,
        float(times[-1]) if times.size else float(len(audio)) / sr,
    )
    ax_spec.set_ylabel("Frequency (kHz)")
    if ax_wave is None:
        ax_spec.set_xlabel("Time (s)")
    if title:
        ax_spec.set_title(title, fontsize=10)

    if settings["grid"]:
        ax_spec.set_yticks(range(0, int(max_freq_khz) + 1, 20))
        ax_spec.set_yticks(range(0, int(max_freq_khz) + 1, 5), minor=True)
        ax_spec.grid(
            True, which="major", axis="y",
            color="#c5d7e6", alpha=0.25, linewidth=0.5,
        )
        ax_spec.grid(
            True, which="minor", axis="y",
            color="#c5d7e6", alpha=0.12, linewidth=0.4,
        )

    cbar = fig.colorbar(pcm, ax=ax_spec, pad=0.01)
    cbar.set_label("Magnitude (dB)", fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    # Overlay detection boxes + species labels only when ``with_boxes=True``.
    if with_boxes:
        for det, pred in detection_pairs:
            start = float(det.get("start_time", 0.0))
            end = float(det.get("end_time", start))
            lo_khz = float(det.get("low_freq", 0.0)) / 1000.0
            hi_khz = float(det.get("high_freq", 0.0)) / 1000.0
            if end <= start or hi_khz <= lo_khz:
                continue
            rect = Rectangle(
                (start, lo_khz), end - start, hi_khz - lo_khz,
                linewidth=1.2, edgecolor="#ff3b3b",
                facecolor="none", alpha=0.95,
            )
            ax_spec.add_patch(rect)
            label = pred.get("predicted_class") or det.get("class", "?")
            conf = pred.get("prediction_confidence", 0.0)
            ax_spec.text(
                start, min(hi_khz + 2.5, max_freq_khz - 2),
                f"{label} {conf:.0%}",
                color="#ff3b3b", fontsize=7.5, weight="bold",
                verticalalignment="bottom",
            )

    # Waveform strip beneath — matches the "amplitude envelope" SonoBat
    # renders in green below its main spec. Helps eyeball where the
    # bat passes actually are without reading the colormap.
    if ax_wave is not None:
        # Decimate hard for display — we don't need 256k samples/sec worth of
        # pixels in a 1200-px strip. max-pooling preserves transient peaks.
        target_pixels = 2000
        if len(audio) > target_pixels * 2:
            step = len(audio) // target_pixels
            usable = len(audio) - (len(audio) % step)
            chunks = audio[:usable].reshape(-1, step)
            audio_peak = np.max(np.abs(chunks), axis=1)
            wave_t = np.linspace(
                0.0, len(audio) / sr, audio_peak.size, endpoint=False,
            )
        else:
            audio_peak = np.abs(audio)
            wave_t = np.linspace(
                0.0, len(audio) / sr, audio_peak.size, endpoint=False,
            )
        # Normalise for display only.
        denom = float(audio_peak.max()) if audio_peak.size else 1.0
        if denom <= 0:
            denom = 1.0
        ax_wave.plot(
            wave_t, audio_peak / denom,
            color="#7dd87d", linewidth=0.6,
        )
        ax_wave.set_ylim(0, 1.05)
        ax_wave.set_xlabel("Time (s)")
        ax_wave.set_yticks([])
        ax_wave.set_facecolor("#0a1020")
        ax_wave.grid(False)
        for spine in ax_wave.spines.values():
            spine.set_color("#444")

    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
