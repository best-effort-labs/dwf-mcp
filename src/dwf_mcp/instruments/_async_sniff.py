"""Shared infrastructure for async observe-mode sniff tools.

This module hosts the plumbing that is identical across
``sniff.spi_*`` and the upcoming ``sniff.{i2c,uart,can}_*`` tools:

1. Claim a DigitalIn ``claim_observe`` allocator slot.
2. Configure + arm DigitalIn record mode.
3. Spawn the background ``record_loop`` task.
4. On stop: cancel the task, drain remaining samples, stop hardware.
5. Provide a retention/reaper pass for sessions that auto-completed but were
   never explicitly stopped by the caller.

Protocol-specific decoding and artifact writing remain in the per-protocol
``sniff`` methods that build on these helpers.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from dwf_mcp.streaming import RecordingSession, record_loop

log = logging.getLogger(__name__)

SNIFF_REAP_AFTER_S = 300.0  # 5 minutes
MAX_RAW_BYTES = 32 * 1024 * 1024  # 32 MB


@dataclass
class _AsyncSniffSession:
    """Internal session record for async observe-mode sniff tools.

    Bundles the underlying :class:`RecordingSession` with the allocator key
    we need to release on stop and the timestamps used by the reaper.
    The ``meta`` dict carries protocol-specific configuration (e.g. SPI mode,
    UART baud) so the corresponding ``*_stop`` method can decode the capture.
    """

    sniff_id: str
    record_session: RecordingSession
    allocator_key: str
    started_at: float
    completed_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # Convenience accessors so callers (and tests written against the
    # previous `RecordingSession`-shaped sessions) can keep using
    # ``session.task`` / ``session.notification_task`` without reaching
    # through ``session.record_session``.
    @property
    def task(self) -> asyncio.Task[None] | None:
        return self.record_session.task

    @property
    def notification_task(self) -> asyncio.Task[None] | None:
        return self.record_session.notification_task


def check_memory_cap(sample_rate_hz: float, max_duration_s: float, n_pins: int) -> None:
    """Raise ``ValueError`` if the projected raw capture exceeds the 32 MB cap.

    The estimate is intentionally pessimistic: it assumes one raw sample byte
    per active pin (record-mode stores one ``uint8`` per channel per sample),
    so it provides a safe upper bound for callers that haven't yet committed
    to a specific encoding.
    """
    bytes_needed = sample_rate_hz * max_duration_s * max(1, n_pins)
    if bytes_needed > MAX_RAW_BYTES:
        suggested = MAX_RAW_BYTES / (sample_rate_hz * max(1, n_pins))
        raise ValueError(
            f"capture would need {bytes_needed/1e6:.1f} MB raw, exceeds 32 MB cap; "
            f"reduce sample_rate_hz or max_duration_s (try max_duration_s<={suggested:.2f})"
        )


def start_observe_session(
    device: Any,
    allocator_key: str,
    pin_mask: int,
    sample_rate_hz: float,
    max_duration_s: float,
    meta: dict[str, Any],
) -> _AsyncSniffSession:
    """Claim DigitalIn observer, arm record mode, spawn ``record_loop`` task.

    Rolls back fully on any partial-setup failure so the caller never has
    to clean up a half-initialised session.

    ``meta`` is stored on both the ``RecordingSession`` and the returned
    :class:`_AsyncSniffSession` so per-protocol ``*_stop`` methods can recover
    their configuration without threading separate state.
    """
    device.allocator.claim_observe(allocator_key)
    try:
        device.backend.logic_record_configure(
            pin_mask=pin_mask,
            sample_rate_hz=sample_rate_hz,
            duration_s=max_duration_s,
        )
        device.backend.logic_record_arm()
    except Exception:
        try:
            device.backend.logic_record_stop()
        except Exception as exc:
            log.warning("logic_record_stop during start-failure cleanup: %s", exc)
        device.allocator.release(allocator_key)
        raise

    sniff_id = meta.get("sniff_id") or str(uuid.uuid4())
    record_session = RecordingSession(
        record_id=sniff_id,
        task=None,
        notification_task=None,
        queue=asyncio.Queue(maxsize=32),
        chunks=[],
        lost_samples=0,
        done=False,
        error=None,
        meta=meta,
    )
    try:
        record_session.task = asyncio.create_task(
            record_loop(
                record_session,
                device.backend.logic_record_status,
                device.backend.logic_record_read,
            )
        )
    except Exception:
        try:
            device.backend.logic_record_stop()
        except Exception as exc:
            log.warning("logic_record_stop during task-create failure: %s", exc)
        device.allocator.release(allocator_key)
        raise

    return _AsyncSniffSession(
        sniff_id=sniff_id,
        record_session=record_session,
        allocator_key=allocator_key,
        started_at=time.monotonic(),
        meta=meta,
    )


def reap_completed_sessions(
    sessions: dict[str, _AsyncSniffSession], device: Any
) -> None:
    """Release allocator claims for auto-stopped sessions older than the
    retention window.

    A session is considered "auto-stopped" once its underlying
    :class:`RecordingSession` reports ``done`` — the first reap pass after
    that stamps ``completed_at``; subsequent passes evict the session
    once ``SNIFF_REAP_AFTER_S`` has elapsed.

    Defensive against partially-initialised or already-cancelled tasks.
    """
    now = time.monotonic()
    for sniff_id, session in list(sessions.items()):
        if session.completed_at is None:
            if session.record_session.done:
                session.completed_at = now
            continue
        if now - session.completed_at >= SNIFF_REAP_AFTER_S:
            log.warning(
                "reaping orphan sniff session %s (auto-stopped %.0fs ago, *_stop never called)",
                sniff_id,
                now - session.completed_at,
            )
            task = session.record_session.task
            if task is not None and not task.done():
                task.cancel()
            notif = session.record_session.notification_task
            if notif is not None and not notif.done():
                notif.cancel()
            try:
                device.backend.logic_record_stop()
            except Exception as exc:
                log.warning("logic_record_stop during reap: %s", exc)
            device.allocator.release(session.allocator_key)
            sessions.pop(sniff_id, None)


async def stop_observe_session(
    session: _AsyncSniffSession, device: Any
) -> tuple[np.ndarray, int]:
    """Cancel the record task, drain remaining samples, stop the hardware.

    Returns ``(samples, lost_samples)`` where ``samples`` is the concatenation
    of all chunks captured so far (zero-row ``(0, 16)`` array if none) and
    ``lost_samples`` is the cumulative loss count reported by the backend.

    The caller is responsible for releasing the allocator claim
    (``device.allocator.release(session.allocator_key)``) once any
    protocol-specific decoding has completed.
    """
    r = session.record_session
    if r.task is not None:
        r.task.cancel()
        with suppress(asyncio.CancelledError):
            await r.task

    try:
        device.backend.logic_record_stop()
    except Exception as exc:
        log.warning("logic_record_stop in stop_observe_session: %s", exc)

    try:
        available, lost, _ = device.backend.logic_record_status()
        r.lost_samples += lost
        if available > 0:
            r.chunks.append(device.backend.logic_record_read(available))
    except Exception as exc:
        log.warning("drain after logic_record_stop: %s", exc)

    samples = (
        np.concatenate(r.chunks, axis=0)
        if r.chunks
        else np.zeros((0, 16), dtype=np.uint8)
    )
    return samples, r.lost_samples
