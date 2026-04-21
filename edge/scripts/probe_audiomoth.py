#!/usr/bin/env python3
"""Infer AudioMoth hardware config (HPF cutoff, gain level) from a live capture.

The AudioMoth USB Microphone firmware doesn't expose its current config
over USB — the gain/filter settings you set in the AudioMoth USB
Microphone App are stored in device flash and only readable via the
app. That means from the Pi we can't ask the device "what gain are
you on?" directly.

But we *can* record audio and infer it. This script captures a short
live sample (default 10s), runs a spectral analysis, and reports:

    * Estimated HPF cutoff (the -3 dB point on the low-frequency side)
    * RMS and peak amplitude (proxy for gain)
    * Noise-floor character (flat across band = mic healthy; dips at
      specific frequencies = filter artifacts)
    * A PNG spectrogram in /tmp for manual review

Intended use: after changing AudioMoth config in the app, unplugging and
replugging the device, run this script to confirm the new settings
actually took effect.

Usage (run from host, with the batdetect-service temporarily stopped
so the audio device is free):

    docker stop edge-batdetect-service-1
    python3 edge/scripts/probe_audiomoth.py
    docker start edge-batdetect-service-1

Or run it inside a one-shot container using the existing image:

    docker run --rm \\
      --device /dev/snd \\
      --privileged \\
      -v /tmp:/host_tmp \\
      edge-batdetect-service:latest \\
      python3 /app/edge/scripts/probe_audiomoth.py --out /host_tmp

This script requires: scipy, numpy, matplotlib. All present in the
batdetect-service image.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict

import numpy as np


DEFAULT_DURATION_SEC = 10
DEFAULT_SAMPLE_RATE = 256000
DEFAULT_OUT_DIR = "/tmp"


def _find_audiomoth_device() -> str:
    """Return the plughw:x,y string for the AudioMoth, or raise."""
    lines = subprocess.check_output(["arecord", "-l"], text=True).splitlines()
    for line in lines:
        if "audiomoth" in line.lower():
            m = re.search(r"card (\d+):.*device (\d+):", line)
            if m:
                return f"plughw:{m.group(1)},{m.group(2)}"
    raise RuntimeError(
        "AudioMoth not found in `arecord -l`. Plug it in, wait for the "
        "red LED, and try again."
    )


def _capture(device: str, duration: int, sample_rate: int, out_path: str) -> None:
    """Record *duration* seconds of audio to *out_path*."""
    cmd = [
        "arecord",
        "-D", device,
        "-f", "S16_LE",
        "-r", str(sample_rate),
        "-c", "1",
        "-d", str(duration),
        "-q",
        out_path,
    ]
    subprocess.check_call(cmd)


@dataclass
class ProbeResult:
    sample_rate: int
    duration_sec: float
    rms: float
    peak: float
    inferred_hpf_hz: float | None
    noise_floor_db: float
    band_energy_db: dict

    def as_report(self) -> str:
        """Human-readable text report."""
        lines = [
            "=" * 60,
            "AudioMoth probe result",
            "=" * 60,
            f"Sample rate        : {self.sample_rate} Hz",
            f"Duration           : {self.duration_sec:.2f} s",
            f"RMS amplitude      : {self.rms:.5f}",
            f"Peak amplitude     : {self.peak:.4f}",
            f"Inferred HPF cutoff: "
            + (f"{self.inferred_hpf_hz/1000:.1f} kHz"
               if self.inferred_hpf_hz else "not detected (broadband content present)"),
            f"Noise floor        : {self.noise_floor_db:.1f} dB",
            "",
            "Band energy (dB, relative to in-band median):",
        ]
        for band, db in self.band_energy_db.items():
            lines.append(f"  {band:>15s} : {db:+.1f} dB")
        lines.append("")
        lines.append(self.as_diagnosis())
        return "\n".join(lines)

    def as_diagnosis(self) -> str:
        """Translate the numbers into a plain-language verdict."""
        out = []

        # Mic alive / gain level
        if self.rms < 0.001:
            out.append("⚠ MIC IS SILENT. No ambient acoustic content detected. "
                       "Check AudioMoth is in CUSTOM position, gain is set, "
                       "and cable + PSU are healthy.")
        elif self.rms < 0.003:
            out.append("⚠ Audio level is very low. Either the gain is set to "
                       "'Low' / 'Medium' in a quiet environment, or the mic "
                       "port is obstructed. Try bumping gain to 'High'.")
        elif self.rms < 0.02:
            out.append("✓ Audio level is normal for outdoor ambient with a "
                       "hardware high-pass filter (8–16 kHz cutoff).")
        elif self.rms < 0.1:
            out.append("✓ Audio level is healthy / slightly loud. Good gain setting.")
        else:
            out.append("⚠ Audio level is very high — clipping risk. Consider "
                       "reducing gain one step.")

        # HPF cutoff
        if self.inferred_hpf_hz is None:
            out.append("⚠ No HPF cutoff detected — low-frequency (< 8 kHz) "
                       "content is reaching the mic. Either no HPF is "
                       "configured, or the switch is in DEFAULT (which "
                       "bypasses the filter). Expected setting: CUSTOM + "
                       "8 kHz HPF.")
        elif self.inferred_hpf_hz < 4000:
            out.append(f"⚠ HPF cutoff detected at {self.inferred_hpf_hz/1000:.1f} kHz, "
                       "lower than expected. Verify AudioMoth config.")
        elif self.inferred_hpf_hz < 20000:
            out.append(f"✓ HPF cutoff inferred at ~{self.inferred_hpf_hz/1000:.1f} kHz — "
                       "consistent with your hardware filter setting.")
        else:
            out.append(f"⚠ HPF cutoff very high ({self.inferred_hpf_hz/1000:.1f} kHz). "
                       "Most bat calls need content above 18 kHz preserved; "
                       "LACI (Hoary Bat) sits at 18–25 kHz. A filter set this "
                       "high will clip real calls.")

        return "\n".join(out)


def _analyze(audio: np.ndarray, sr: int, dur: float) -> ProbeResult:
    """Compute the probe metrics from a mono audio array in [-1, 1]."""
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))

    # Welch-style power spectrum (2 ms window, 75 % overlap — matches
    # batdetect-service's validator).
    nperseg = max(int(0.002 * sr), 64)
    noverlap = int(0.75 * nperseg)
    from scipy.signal import welch
    freqs, psd = welch(audio, fs=sr, nperseg=nperseg, noverlap=noverlap)

    psd_db = 10 * np.log10(np.maximum(psd, 1e-20))
    # Noise floor estimate = median of PSD in the 20-80 kHz "bat band"
    bat_mask = (freqs >= 20000) & (freqs <= 80000)
    noise_floor_db = float(np.median(psd_db[bat_mask])) if bat_mask.any() else 0.0

    # HPF cutoff inference: find the lowest frequency where PSD first
    # exceeds noise_floor - 3 dB. If content is strong at very low
    # frequencies, there's no HPF. If there's a clear rising edge
    # above some frequency, that's the cutoff.
    target_db = float(np.max(psd_db[bat_mask])) - 3.0 if bat_mask.any() else noise_floor_db
    # Walk from DC upward, find where PSD transitions from "below" to
    # "above" the -3 dB threshold relative to the bat-band peak.
    above = psd_db >= target_db
    inferred_hpf_hz: float | None = None
    if above.any():
        # First index where it's above. If that's the very first bin
        # (DC-ish), no HPF is detected.
        first_above = int(np.argmax(above))
        if first_above > 3:  # ignore the first few bins near DC
            inferred_hpf_hz = float(freqs[first_above])

    # Band-energy summary relative to bat-band median
    band_defs = {
        "0–2 kHz":     (0,     2000),
        "2–8 kHz":     (2000,  8000),
        "8–15 kHz":    (8000,  15000),
        "15–25 kHz":   (15000, 25000),
        "25–50 kHz":   (25000, 50000),
        "50–100 kHz":  (50000, 100000),
        "100–128 kHz": (100000, 128000),
    }
    bat_median_linear = float(np.median(psd[bat_mask])) if bat_mask.any() else 1e-20
    band_energy_db = {}
    for name, (lo, hi) in band_defs.items():
        m = (freqs >= lo) & (freqs < hi)
        if m.any():
            band_energy_db[name] = float(
                10 * np.log10(np.maximum(np.median(psd[m]), 1e-20) / bat_median_linear)
            )

    return ProbeResult(
        sample_rate=sr,
        duration_sec=dur,
        rms=rms,
        peak=peak,
        inferred_hpf_hz=inferred_hpf_hz,
        noise_floor_db=noise_floor_db,
        band_energy_db=band_energy_db,
    )


def _spectrogram_png(audio: np.ndarray, sr: int, out_path: str) -> None:
    """Save a magma spectrogram covering 0–min(sr/2, 120) kHz."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.signal import spectrogram

    nperseg = max(int(0.002 * sr), 64)
    noverlap = int(0.75 * nperseg)
    f, t, Sxx = spectrogram(
        audio, fs=sr, window="hann",
        nperseg=nperseg, noverlap=noverlap, mode="magnitude",
    )
    Sxx_db = 20 * np.log10(np.maximum(Sxx, 1e-12))
    vmin, vmax = np.percentile(Sxx_db, [5, 99])
    fig, ax = plt.subplots(figsize=(14, 6))
    im = ax.pcolormesh(t, f / 1000, Sxx_db, shading="auto",
                       vmin=vmin, vmax=vmax, cmap="magma")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (kHz)")
    ax.set_title("AudioMoth probe — spectrogram")
    ax.set_ylim(0, min(sr / 2 / 1000, 120))
    ax.axhline(16, color="cyan", linestyle="--", linewidth=0.7, alpha=0.6,
               label="16 kHz software HPF cutoff")
    ax.legend(loc="upper right", fontsize=8)
    fig.colorbar(im, ax=ax, label="dB")
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION_SEC,
                        help=f"seconds to capture (default {DEFAULT_DURATION_SEC})")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE,
                        help=f"capture rate in Hz (default {DEFAULT_SAMPLE_RATE})")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR,
                        help=f"output directory for PNG + WAV (default {DEFAULT_OUT_DIR})")
    parser.add_argument("--keep-wav", action="store_true",
                        help="keep the temporary WAV instead of deleting after analysis")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    try:
        device = _find_audiomoth_device()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"[probe] capturing {args.duration}s from {device} @ {args.sample_rate} Hz...",
          file=sys.stderr)

    wav_path = os.path.join(args.out, "audiomoth_probe.wav")
    try:
        _capture(device, args.duration, args.sample_rate, wav_path)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: arecord failed (exit {e.returncode}). "
              "Is another process holding the device? "
              "Try `docker stop edge-batdetect-service-1`.", file=sys.stderr)
        return 2

    from scipy.io import wavfile
    sr, raw = wavfile.read(wav_path)
    if raw.ndim > 1:
        raw = raw[:, 0]
    if raw.dtype.kind == "i":
        audio = raw.astype(np.float32) / np.iinfo(raw.dtype).max
    else:
        audio = raw.astype(np.float32)

    duration = len(audio) / sr
    result = _analyze(audio, sr, duration)

    png_path = os.path.join(args.out, "audiomoth_probe.png")
    _spectrogram_png(audio, sr, png_path)

    print()
    print(result.as_report())
    print()
    print(f"Spectrogram PNG : {png_path}")
    if args.keep_wav:
        print(f"WAV (kept)      : {wav_path}")
    else:
        try:
            os.remove(wav_path)
        except OSError:
            pass

    # Emit a machine-readable JSON line on stdout at the very end so
    # callers can grep for it:
    import json
    print("PROBE_JSON " + json.dumps(asdict(result)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
