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

from dwf_mcp.streaming import (
    RecordingSession,
    compute_poll_interval,
    process_chunk,
    record_loop,
)

log = logging.getLogger(__name__)

SNIFF_REAP_AFTER_S = 300.0  # 5 minutes
MAX_RAW_BYTES = 32 * 1024 * 1024  # 32 MB
# Record mode always stores the full 16-bit digital bank — one uint8 per channel
# per sample (see PydwfBackend.logic_record_read) — regardless of how many pins a
# protocol actually decodes. Memory scales with this width, not with n_pins.
BYTES_PER_SAMPLE = 16


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
    decoder: Any | None = None
    # decoder: protocol decoder instance. Set during start when stream_decode=True
    # (decoder lives the entire capture lifetime); set just-before-stop in the
    # accumulation path (constructed locally in *_stop).
    streaming_decode: bool = False
    # streaming_decode: True iff stream_observe_session should read from
    # session.transactions rather than iterating record_session.chunks.
    transactions: list[Any] = field(default_factory=list)
    # transactions: per-chunk decoder output accumulated during stream-mode capture.
    # Empty when streaming_decode=False.

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

    Record mode stores the full 16-bit digital bank (``BYTES_PER_SAMPLE`` bytes
    per sample) regardless of ``n_pins``, so the bound is width-independent.
    ``n_pins`` is accepted for call-site clarity but does not scale the estimate.
    """
    bytes_needed = sample_rate_hz * max_duration_s * BYTES_PER_SAMPLE
    if bytes_needed > MAX_RAW_BYTES:
        suggested = MAX_RAW_BYTES / (sample_rate_hz * BYTES_PER_SAMPLE)
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
    decoder: Any | None = None,
) -> _AsyncSniffSession:
    """Claim DigitalIn observer, arm record mode, spawn ``record_loop`` task.

    Rolls back fully on any partial-setup failure so the caller never has
    to clean up a half-initialised session.

    ``meta`` is stored on both the ``RecordingSession`` and the returned
    :class:`_AsyncSniffSession` so per-protocol ``*_stop`` methods can recover
    their configuration without threading separate state.

    When ``decoder`` is provided, ``on_chunk_sync`` is installed so that the live
    capture loop feeds each chunk through ``decoder.feed(...)`` and appends the
    result to ``session.transactions``. ``_AsyncSniffSession.streaming_decode``
    is set to True so the corresponding stop uses the streaming branch.
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
    session = _AsyncSniffSession(
        sniff_id=sniff_id,
        record_session=record_session,
        allocator_key=allocator_key,
        started_at=time.monotonic(),
        meta=meta,
        decoder=decoder,
        streaming_decode=(decoder is not None),
    )
    if decoder is not None:
        # NOTE: closure captures `session` and `decoder` via default-arg binding to
        # avoid late-binding surprises. Decoder exceptions from feed() propagate out
        # of process_chunk → record_loop catches them, sets r.error + r.done. Do NOT
        # swallow here.
        record_session.on_chunk_sync = (
            lambda chunk, _s=session, _d=decoder: _s.transactions.extend(_d.feed(chunk))
        )
    poll_interval_s = compute_poll_interval(
        device.require_open().digital_in_buffer_max, sample_rate_hz
    )
    try:
        record_session.task = asyncio.create_task(
            record_loop(
                record_session,
                device.backend.logic_record_status,
                device.backend.logic_record_read,
                poll_interval_s=poll_interval_s,
            )
        )
    except Exception:
        try:
            device.backend.logic_record_stop()
        except Exception as exc:
            log.warning("logic_record_stop during task-create failure: %s", exc)
        device.allocator.release(allocator_key)
        raise

    return session


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


async def _quiesce_and_drain(
    session: _AsyncSniffSession, device: Any
) -> None:
    """Cancel the background record task, stop hardware, and drain any final
    samples into ``session.record_session.chunks``. Used by
    ``stream_observe_session`` (per-chunk decode path)."""
    r = session.record_session
    if r.task is not None:
        r.task.cancel()
        with suppress(asyncio.CancelledError):
            await r.task

    try:
        device.backend.logic_record_stop()
    except Exception as exc:
        log.warning("logic_record_stop in _quiesce_and_drain: %s", exc)

    # Split error handling: backend status/read errors are log-and-continue (we already
    # have what we have); but callback errors from process_chunk MUST surface via r.error
    # so the streaming-mode caller can detect a decoder failure during final drain.
    chunk = None
    try:
        available, lost, _ = device.backend.logic_record_status()
        r.lost_samples += lost
        if available > 0:
            chunk = device.backend.logic_record_read(available)
    except Exception as exc:
        log.warning("drain after logic_record_stop: %s", exc)
    if chunk is not None:
        try:
            process_chunk(r, chunk)
        except Exception as exc:
            r.error = str(exc)
            r.done = True


async def stream_observe_session(
    session: _AsyncSniffSession,
    device: Any,
) -> tuple[list[Any], int]:
    """Cancel the record task, stop hardware, and produce decoded transactions.

    The decoder lives on ``session.decoder``. In stream mode (set during
    ``start_observe_session`` when a decoder was passed), transactions were
    already accumulated chunk-by-chunk in the live capture loop via
    ``on_chunk_sync``; this method just finalises. In accumulation mode (the
    default), this method iterates the buffered chunks and feeds them one at
    a time, mirroring the historic behaviour of this function before the
    streaming path was added.

    Returns ``(transactions, lost_samples)``. Raises ``RuntimeError`` if
    the capture path recorded an error (e.g. decoder.feed raised mid-stream).
    """
    await _quiesce_and_drain(session, device)
    r = session.record_session
    if r.error is not None:
        raise RuntimeError(f"sniff capture failed: {r.error}")
    if session.decoder is None:
        raise RuntimeError("stream_observe_session called without session.decoder set")
    transactions: list[Any] = []
    if session.streaming_decode:
        transactions.extend(session.transactions)
        transactions.extend(session.decoder.finalize())
    else:
        for chunk in r.chunks:
            transactions.extend(session.decoder.feed(chunk))
        transactions.extend(session.decoder.finalize())
        r.chunks.clear()
    return transactions, r.lost_samples
