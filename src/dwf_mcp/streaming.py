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
    # When set, chunks are NOT appended to session.chunks (write-through for VCD).
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)


def process_chunk(session: RecordingSession, chunk: np.ndarray) -> None:
    if session.on_chunk_sync is not None:
        session.on_chunk_sync(chunk)
    else:
        session.chunks.append(chunk)


async def record_loop(
    session: RecordingSession,
    poll_fn: Callable[[], tuple[int, int, int]],
    read_fn: Callable[[int], np.ndarray],
) -> None:
    try:
        while not session.done:
            await asyncio.sleep(0.010)
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
                try:
                    session.queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass  # notification dropped; recording continues unaffected
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
