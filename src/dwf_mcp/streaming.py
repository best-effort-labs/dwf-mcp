"""Shared streaming infrastructure for Logic and Scope record modes."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


@dataclasses.dataclass
class RecordingSession:
    record_id: str
    task: asyncio.Task[None] | None
    notification_task: asyncio.Task[None] | None
    queue: asyncio.Queue[np.ndarray | None]
    chunks: list[np.ndarray]
    lost_samples: int
    done: bool
    error: str | None
    on_chunk: Callable[[str, np.ndarray], Awaitable[None]] | None = None
    # on_chunk: async callback for lifecycle tracking (spec-required for MCP notifications).
    on_chunk_sync: Callable[[np.ndarray], None] | None = None
    # on_chunk_sync: called synchronously per chunk before queue put.
    # When set, chunks are NOT appended to session.chunks (write-through for VCD
    # or live-decode sniff). SINGLE-OWNER: only one consumer at a time (no
    # callback composition); callers that need to share must build their own
    # multiplexing layer. Today: VCD writer OR sniff streaming, never both on
    # the same session.
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    # samples_received: cumulative samples seen by process_chunk; source of truth
    # for status reporting (accumulation- AND stream-mode).
    samples_received: int = 0


def process_chunk(session: RecordingSession, chunk: np.ndarray) -> None:
    session.samples_received += len(chunk)
    if session.on_chunk_sync is not None:
        session.on_chunk_sync(chunk)
    else:
        session.chunks.append(chunk)


_MAX_POLL_INTERVAL_S = 0.010
_MIN_POLL_INTERVAL_S = 0.001
# Read when the record buffer is at most this full, leaving headroom for read +
# decode time before the next poll.
_POLL_FILL_FRACTION = 0.4


def compute_poll_interval(buffer_size: int, sample_rate_hz: float) -> float:
    """Pick a record-loop poll interval that can't let the device buffer overflow.

    The buffer fills in ``buffer_size / sample_rate`` seconds; we poll at a
    fraction of that (clamped to [1 ms, 10 ms]). Small-buffer devices (the
    original Analog Discovery has a 4096-sample DigitalIn buffer vs the AD3's
    16384) poll faster so they don't drop samples; slow captures stay at the
    10 ms default.
    """
    if sample_rate_hz <= 0 or buffer_size <= 0:
        return _MAX_POLL_INTERVAL_S
    fill_time = buffer_size / sample_rate_hz
    return max(_MIN_POLL_INTERVAL_S, min(_MAX_POLL_INTERVAL_S, fill_time * _POLL_FILL_FRACTION))


async def record_loop(
    session: RecordingSession,
    poll_fn: Callable[[], tuple[int, int, int]],
    read_fn: Callable[[int], np.ndarray],
    poll_interval_s: float = _MAX_POLL_INTERVAL_S,
) -> None:
    try:
        while not session.done:
            await asyncio.sleep(poll_interval_s)
            available, lost, remaining = poll_fn()
            session.lost_samples += lost
            if available > 0:
                chunk = read_fn(available)
                try:
                    process_chunk(session, chunk)
                except Exception as exc:
                    session.error = str(exc)
                    session.done = True
                    return
                # notification dropped on a full queue; recording continues unaffected
                with contextlib.suppress(asyncio.QueueFull):
                    session.queue.put_nowait(chunk)
            if remaining == 0:
                session.done = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        session.error = str(exc)
        session.done = True
    finally:
        # Best-effort sentinel; record_stop cancels notification_task explicitly.
        with contextlib.suppress(asyncio.QueueFull):
            session.queue.put_nowait(None)


async def notification_loop(
    session: RecordingSession,
    on_chunk: Callable[[str, np.ndarray], Awaitable[None]],
) -> None:
    while True:
        item = await session.queue.get()
        if item is None:
            break
        try:
            await on_chunk(session.record_id, item)
        except Exception:  # swallow; never block recording
            log.warning("notification send failed for record_id=%r", session.record_id)


async def flush_pending_notifications(
    session: RecordingSession, final_chunks: list[np.ndarray]
) -> None:
    """Deliver to ``on_chunk`` any queue items the notification loop didn't get to
    (because record_stop cancelled it), followed by the final chunks drained at
    stop, in order.

    Called from record_stop *after* the notification task is cancelled. Items the
    notification loop already delivered were removed from the queue, so there is no
    double-send; the final drained chunks never entered the queue, so they are not
    duplicated either. A no-op when the session has no async ``on_chunk`` consumer.
    """
    if session.on_chunk is None:
        return
    pending: list[np.ndarray] = []
    while True:
        try:
            item = session.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is not None:  # skip the record_loop end-of-stream sentinel
            pending.append(item)
    for chunk in (*pending, *final_chunks):
        try:
            await session.on_chunk(session.record_id, chunk)
        except Exception:  # swallow; matches notification_loop semantics
            log.warning("final notification send failed for record_id=%r", session.record_id)
