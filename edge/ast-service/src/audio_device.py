import asyncio
import fcntl
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, AsyncGenerator

import librosa
import numpy as np

LOCK_PATH = "/locks/audio_device.lock"


class AudioDevice:
    def __init__(self, name: str, channels: int = 1,
                 sampling_rate: int = 192000, format: str = "S16_LE"):
        self.name = self._match_device(name)
        self.channels = channels
        self.sampling_rate = sampling_rate
        self.format = format

    @staticmethod
    def _match_device(name: str) -> str:
        lines = subprocess.check_output(['arecord', '-l'], text=True).splitlines()
        devices = [
            f'plughw:{m.group(1)},{m.group(2)}'
            for line in lines
            if name.lower() in line.lower()
            if (m := re.search(r'card (\d+):.*device (\d+):', line))
        ]
        if len(devices) == 0:
            raise ValueError(f'No devices found matching `{name}`')
        if len(devices) > 1:
            raise ValueError(f'Multiple devices found matching `{name}` -> {devices}')
        return devices[0]

    async def continuous_capture(
        self, sample_duration: int = 1, capture_delay: int = 0
    ) -> AsyncGenerator[np.ndarray, Any]:
        with TemporaryDirectory() as temp_dir:
            temp_file = f'{temp_dir}/audio.wav'
            command = (
                f'arecord -d {sample_duration} -D {self.name} '
                f'-f {self.format} -r {self.sampling_rate} '
                f'-c {self.channels} -q {temp_file}'
            )
            while True:
                lock_fd = open(LOCK_PATH, 'w')
                try:
                    # Block until we acquire the exclusive lock
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    subprocess.check_call(command, shell=True)
                except subprocess.CalledProcessError:
                    print('[AST] Audio capture failed, retrying in 2s')
                    await asyncio.sleep(2)
                    continue
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()

                data, sr = librosa.load(temp_file, sr=self.sampling_rate)
                await asyncio.sleep(capture_delay)
                yield data
