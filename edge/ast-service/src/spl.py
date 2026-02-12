import warnings

import numpy as np
from maad.spl import wav2dBSPL
from maad.util import mean_dB


async def calculate_sound_pressure_level(
    audio: np.ndarray, gain: float = 25, sensitivity: float = -18
) -> float:
    # Add tiny epsilon to avoid log10(0) warnings
    audio_safe = np.where(audio == 0, 1e-10, audio)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        x = wav2dBSPL(audio_safe, gain=gain, sensitivity=sensitivity, Vadc=1.25)
        return float(mean_dB(x, axis=0))
