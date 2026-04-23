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

# Tuning iteration 4 (2026-04-23): compared side-by-side with the real
# SonoBat reference. Iteration 3 was still too cyan-dominated — the
# brightness extended up the whole vertical arch of each call, whereas
# SonoBat keeps most of the call body a saturated royal blue and only
# brightens to cyan/white at the thick end where the FM sweep bends.
#
# The ramp is now heavily blue-weighted: ambient speckle sits in deep
# navy, the bulk of each call body stays in royal/pure blue, and only
# the hottest few percent of pixels light up as cyan+cream-yellow
# (matching the warm highlight SonoBat puts on call tips).
_SONOBAT_STOPS = [
    # Iteration 5 (2026-04-23): the bright band was still spreading
    # beyond the call tips. Pushed the cyan + cream highlights from
    # 0.95–1.00 down to 0.985–1.00 so only the top ~1.5% of pixels
    # brighten. Result: calls stay blue with just a pinprick warm tip
    # at the FM hook, matching the reference.
    (0.00, "#000206"),  # near-black
    (0.15, "#020616"),  # very dark blue-black (quiet)
    (0.35, "#06114a"),  # dark navy (noise speckle floor)
    (0.58, "#0d2090"),  # deep royal blue (noise + faint calls)
    (0.80, "#1c3fd8"),  # saturated blue (typical call body)
    (0.94, "#3870ff"),  # bright pure blue (call peak mid — most of tip)
    (0.985, "#a8c8ff"), # soft cyan — only the very top 1.5%
    (1.00, "#fff4c8"),  # pale cream (hottest pixel only — pinprick at FM hook)
]


def _palette_settings(palette: str) -> dict:
    """Return the STFT + display settings appropriate for the palette."""
    if palette == "sonobat":
        return {
            "nperseg_sec": 0.0015,   # 1.5 ms — fine time resolution so
                                     # each call's downward FM sweep is
                                     # visible as a distinct curved stripe
            "overlap": 0.80,         # iteration 3 (2026-04-23): 0.90 was
                                     # generating ~98k STFT frames on a
                                     # 15s clip and timing out the CF at
                                     # render time. 0.80 halves the frame
                                     # count while keeping sweeps smooth.
            "dynamic_range_db": 36,  # iteration 3 (2026-04-23): widened
                                     # from 30 so ambient noise floor has
                                     # visible texture instead of clipping
                                     # to the bottom palette stop. Pairs
                                     # with the softer top stop to stop
                                     # call peaks glowing as neon.
            "cmap_name": "sonobat",
            "grid": False,         # iteration 4 (2026-04-23): the real
                                   # SonoBat reference has no horizontal
                                   # grid lines. Earlier 5/20 kHz grid
                                   # was our addition — dropped to match.
            "waveform_strip": False,  # iteration 4 (2026-04-23): green
                                      # envelope strip dropped — not in
                                      # the SonoBat reference and not
                                      # adding decision-useful info.
            "max_display_cols": 2000,  # max-pool the time axis before
                                       # pcolormesh so render cost stays
                                       # bounded regardless of input length.
                                       # Max-pool (not mean) keeps bright
                                       # call peaks intact.
        }
    # viridis default — what was already shipping
    return {
        "nperseg_sec": 0.004,
        "overlap": 0.75,
        "dynamic_range_db": 80,
        "cmap_name": "viridis",
        "grid": False,
        "waveform_strip": False,
        "max_display_cols": 2500,  # same safety cap as sonobat; viridis
                                   # STFT is coarser so rarely triggers
                                   # downsampling, but caps pathological
                                   # long inputs either way.
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

    # Max-pool the time axis so pcolormesh doesn't choke on dense
    # high-overlap STFTs. We max-pool (not mean) on purpose — bat calls
    # are short bright transients, and averaging would smear them into
    # the surrounding quiet. 2000 columns is plenty for a 1200 px image.
    max_cols = int(settings.get("max_display_cols", 2500))
    if Sxx_db.shape[1] > max_cols:
        chunk = Sxx_db.shape[1] // max_cols
        usable = max_cols * chunk
        Sxx_db = (
            Sxx_db[:, :usable]
            .reshape(Sxx_db.shape[0], max_cols, chunk)
            .max(axis=2)
        )
        times = times[:usable:chunk]

    # Iteration 5 (2026-04-23): sonobat uses a higher percentile so
    # only the truly hottest pixels anchor the top of the palette —
    # combined with the 0.985–1.00 bright band this confines the
    # cream highlight to pinpricks at the FM hook instead of spreading
    # across the call body.
    vmax_percentile = 99.7 if settings["cmap_name"] == "sonobat" else 99.0
    vmax = float(np.percentile(Sxx_db, vmax_percentile))
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
