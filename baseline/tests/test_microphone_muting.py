import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.audio.capture import MicrophoneStream


def _fake_frame(frame_samples: int) -> np.ndarray:
    return np.zeros((frame_samples, 1), dtype=np.int16)


async def test_callback_enqueues_frames_when_not_muted():
    mic = MicrophoneStream(sample_rate=16000, frame_samples=1280)
    mic._loop = asyncio.get_running_loop()

    mic._callback(_fake_frame(1280), 1280, None, None)
    await asyncio.sleep(0)  # let the scheduled call_soon_threadsafe callback run

    assert mic._queue.qsize() == 1
    assert mic.muted is False


async def test_callback_drops_frames_at_source_when_muted():
    mic = MicrophoneStream(sample_rate=16000, frame_samples=1280)
    mic._loop = asyncio.get_running_loop()

    mic.set_muted(True)
    mic._callback(_fake_frame(1280), 1280, None, None)
    await asyncio.sleep(0)

    assert mic._queue.qsize() == 0
    assert mic._muted_frames_dropped == 1
    assert mic.muted is True


async def test_unmuting_resumes_normal_capture():
    mic = MicrophoneStream(sample_rate=16000, frame_samples=1280)
    mic._loop = asyncio.get_running_loop()

    mic.set_muted(True)
    mic._callback(_fake_frame(1280), 1280, None, None)
    await asyncio.sleep(0)

    mic.set_muted(False)
    mic._callback(_fake_frame(1280), 1280, None, None)
    await asyncio.sleep(0)

    assert mic._queue.qsize() == 1  # only the unmuted frame made it through
    assert mic._muted_frames_dropped == 1


async def test_drain_clears_frames_queued_before_a_mute_toggle():
    mic = MicrophoneStream(sample_rate=16000, frame_samples=1280)
    mic._loop = asyncio.get_running_loop()

    mic._callback(_fake_frame(1280), 1280, None, None)
    mic._callback(_fake_frame(1280), 1280, None, None)
    await asyncio.sleep(0)
    assert mic._queue.qsize() == 2

    mic.drain()

    assert mic._queue.qsize() == 0
