# tests/unit/test_streaming.py
"""Unit tests for streaming.py."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import numpy as np
import pytest

from dwf_mcp.streaming import RecordingSession, notification_loop, process_chunk, record_loop


def _session(**kwargs: Any) -> RecordingSession:
    return RecordingSession(
        record_id=str(uuid.uuid4()),
        task=None,
        notification_task=None,
        queue=asyncio.Queue(maxsize=32),
        chunks=[],
        lost_samples=0,
        done=False,
        error=None,
        **kwargs,
    )


def test_process_chunk_accumulates_by_default() -> None:
    s = _session()
    chunk = np.array([[1, 0], [0, 1]], dtype=np.uint8)
    process_chunk(s, chunk)
    assert len(s.chunks) == 1
    np.testing.assert_array_equal(s.chunks[0], chunk)


def test_process_chunk_calls_on_chunk_sync_and_skips_accumulation() -> None:
    received: list[np.ndarray] = []
    s = _session(on_chunk_sync=received.append)
    chunk = np.array([[1, 0]], dtype=np.uint8)
    process_chunk(s, chunk)
    assert len(received) == 1
    assert len(s.chunks) == 0


def test_process_chunk_on_chunk_sync_exception_propagates() -> None:
    def boom(c: np.ndarray) -> None:
        raise ValueError("disk full")
    s = _session(on_chunk_sync=boom)
    with pytest.raises(ValueError, match="disk full"):
        process_chunk(s, np.zeros((2, 2), dtype=np.uint8))


@pytest.mark.asyncio
async def test_record_loop_collects_chunks_and_terminates_on_remaining_zero() -> None:
    calls = 0
    def poll_fn() -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (4, 0, 1)
        return (0, 0, 0)
    def read_fn(n: int) -> np.ndarray:
        return np.zeros((n, 2), dtype=np.uint8)
    s = _session()
    await asyncio.create_task(record_loop(s, poll_fn, read_fn))
    assert s.done is True
    assert s.error is None
    assert len(s.chunks) == 1
    assert s.chunks[0].shape == (4, 2)


@pytest.mark.asyncio
async def test_record_loop_accumulates_lost_samples() -> None:
    calls = 0
    def poll_fn() -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (2, 5, 1)
        return (0, 3, 0)
    def read_fn(n: int) -> np.ndarray:
        return np.zeros((n, 2), dtype=np.uint8)
    s = _session()
    await asyncio.create_task(record_loop(s, poll_fn, read_fn))
    assert s.lost_samples == 8


@pytest.mark.asyncio
async def test_record_loop_puts_chunk_on_queue() -> None:
    calls = 0
    def poll_fn() -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (3, 0, 0)
        return (0, 0, 0)
    def read_fn(n: int) -> np.ndarray:
        return np.zeros((n, 2), dtype=np.uint8)
    s = _session()
    await asyncio.create_task(record_loop(s, poll_fn, read_fn))
    item = s.queue.get_nowait()
    assert item is not None
    assert item.shape == (3, 2)


@pytest.mark.asyncio
async def test_record_loop_drops_notification_when_queue_full() -> None:
    s = _session()
    for _ in range(32):
        s.queue.put_nowait(np.zeros((1, 2), dtype=np.uint8))
    calls = 0
    def poll_fn() -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (1, 0, 0)
        return (0, 0, 0)
    def read_fn(n: int) -> np.ndarray:
        return np.zeros((n, 2), dtype=np.uint8)
    await asyncio.create_task(record_loop(s, poll_fn, read_fn))
    assert s.error is None
    assert len(s.chunks) == 1


@pytest.mark.asyncio
async def test_record_loop_on_chunk_sync_error_sets_error_and_done() -> None:
    def boom(c: np.ndarray) -> None:
        raise OSError("write failed")
    calls = 0
    def poll_fn() -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        return (1, 0, 1 if calls < 2 else 0)
    def read_fn(n: int) -> np.ndarray:
        return np.zeros((n, 2), dtype=np.uint8)
    s = _session(on_chunk_sync=boom)
    await asyncio.create_task(record_loop(s, poll_fn, read_fn))
    assert s.error == "write failed"
    assert s.done is True


@pytest.mark.asyncio
async def test_record_loop_backend_exception_sets_error() -> None:
    def poll_fn() -> tuple[int, int, int]:
        raise RuntimeError("hardware gone")
    def read_fn(n: int) -> np.ndarray:
        return np.zeros((n, 2), dtype=np.uint8)
    s = _session()
    await asyncio.create_task(record_loop(s, poll_fn, read_fn))
    assert "hardware gone" in (s.error or "")
    assert s.done is True


@pytest.mark.asyncio
async def test_record_loop_cancellation_propagates() -> None:
    def poll_fn() -> tuple[int, int, int]:
        return (0, 0, 1)
    def read_fn(n: int) -> np.ndarray:
        return np.zeros((n, 2), dtype=np.uint8)
    s = _session()
    task = asyncio.create_task(record_loop(s, poll_fn, read_fn))
    await asyncio.sleep(0.025)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_notification_loop_delivers_chunks_and_stops_on_sentinel() -> None:
    delivered: list[tuple[str, np.ndarray]] = []
    async def on_chunk(record_id: str, chunk: np.ndarray) -> None:
        delivered.append((record_id, chunk))
    s = _session()
    c1, c2 = np.zeros((4, 2), dtype=np.uint8), np.ones((2, 2), dtype=np.uint8)
    s.queue.put_nowait(c1)
    s.queue.put_nowait(c2)
    s.queue.put_nowait(None)
    await notification_loop(s, on_chunk)
    assert len(delivered) == 2
    np.testing.assert_array_equal(delivered[0][1], c1)
    np.testing.assert_array_equal(delivered[1][1], c2)


@pytest.mark.asyncio
async def test_notification_loop_exception_does_not_stop_loop() -> None:
    call_count = 0
    delivered: list[np.ndarray] = []
    async def on_chunk(record_id: str, chunk: np.ndarray) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("send failed")
        delivered.append(chunk)
    s = _session()
    s.queue.put_nowait(np.zeros((1, 2), dtype=np.uint8))
    s.queue.put_nowait(np.ones((1, 2), dtype=np.uint8))
    s.queue.put_nowait(None)
    await notification_loop(s, on_chunk)
    assert len(delivered) == 1
    assert s.error is None
