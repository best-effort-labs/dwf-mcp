"""Adaptive record-loop poll interval (keeps small-buffer devices from dropping
samples in streaming/record mode)."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from dwf_mcp.streaming import RecordingSession, compute_poll_interval, record_loop


def test_poll_interval_small_buffer_polls_faster_than_fill() -> None:
    # AD1: 4096-sample buffer at 1 MHz fills in ~4.1 ms; we must poll faster.
    interval = compute_poll_interval(4096, 1_000_000)
    fill_time = 4096 / 1_000_000
    assert interval < fill_time
    assert interval == pytest.approx(fill_time * 0.4)


def test_poll_interval_large_buffer() -> None:
    # AD3: 16384-sample buffer at 1 MHz -> ~6.55 ms.
    assert compute_poll_interval(16384, 1_000_000) == pytest.approx(0.0065536)


def test_poll_interval_clamped_to_bounds() -> None:
    assert compute_poll_interval(16384, 100_000) == 0.010   # slow capture: capped
    assert compute_poll_interval(100, 1_000_000) == 0.001    # tiny buffer: floored
    assert compute_poll_interval(0, 1_000_000) == 0.010      # guard
    assert compute_poll_interval(4096, 0) == 0.010           # guard


def _session() -> RecordingSession:
    return RecordingSession(
        record_id="r", task=None, notification_task=None,
        queue=asyncio.Queue(maxsize=32), chunks=[], lost_samples=0,
        done=False, error=None,
    )


@pytest.mark.asyncio
async def test_record_loop_uses_passed_poll_interval(monkeypatch) -> None:
    import dwf_mcp.streaming as streaming
    slept: list[float] = []

    async def fake_sleep(t: float) -> None:
        slept.append(t)

    monkeypatch.setattr(streaming.asyncio, "sleep", fake_sleep)
    session = _session()
    # poll_fn reports done on the first poll (remaining == 0).
    await record_loop(session, lambda: (0, 0, 0),
                      lambda n: np.zeros((n, 16), dtype=np.uint8),
                      poll_interval_s=0.002)
    assert 0.002 in slept
