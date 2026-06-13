"""Unit tests for shared async-sniff infrastructure."""
from __future__ import annotations

import asyncio
import time

import pytest

from dwf_mcp.instruments._async_sniff import (
    BYTES_PER_SAMPLE,
    MAX_RAW_BYTES,
    SNIFF_REAP_AFTER_S,
    _AsyncSniffSession,
    check_memory_cap,
    reap_completed_sessions,
)

# --- check_memory_cap ---

def test_check_memory_cap_passes_under_limit() -> None:
    """16 MB raw: 1 MHz x 1 s x 16 bytes/sample. Under the 32 MB cap."""
    check_memory_cap(sample_rate_hz=1_000_000.0, max_duration_s=1.0, n_pins=2)


def test_check_memory_cap_raises_over_limit() -> None:
    """1.6 GB raw: 100 MHz x 1 s x 16 bytes/sample. Far over 32 MB cap."""
    with pytest.raises(ValueError, match="32 MB"):
        check_memory_cap(sample_rate_hz=100_000_000.0, max_duration_s=1.0, n_pins=2)


def test_check_memory_cap_exact_boundary() -> None:
    """A value exactly at the cap should pass.

    The helper raises only when ``bytes_needed > MAX_RAW_BYTES`` (strict),
    so hitting the cap on the nose must succeed. Storage is BYTES_PER_SAMPLE
    bytes/sample, so the boundary is rate * duration * BYTES_PER_SAMPLE == cap.
    """
    rate = MAX_RAW_BYTES / BYTES_PER_SAMPLE  # 1 s * 16 bytes/sample * rate == cap
    check_memory_cap(sample_rate_hz=rate, max_duration_s=1.0, n_pins=1)
    # Sanity: confirm we sit exactly at the cap.
    assert rate * 1.0 * BYTES_PER_SAMPLE == MAX_RAW_BYTES


def test_check_memory_cap_suggests_smaller_duration() -> None:
    """ValueError message should suggest a smaller max_duration_s the user can use."""
    with pytest.raises(ValueError) as exc_info:
        check_memory_cap(sample_rate_hz=10_000_000.0, max_duration_s=10.0, n_pins=2)
    assert "max_duration" in str(exc_info.value).lower()


# --- reap_completed_sessions ---

class _FakeAllocator:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, key: str) -> None:
        self.released.append(key)


class _FakeBackend:
    def __init__(self) -> None:
        self.stop_calls = 0

    def logic_record_stop(self) -> None:
        self.stop_calls += 1


class _FakeDevice:
    def __init__(self) -> None:
        self.allocator = _FakeAllocator()
        self.backend = _FakeBackend()


def _make_completed_session(
    sniff_id: str,
    completed_at: float,
    allocator_key: str | None = None,
) -> _AsyncSniffSession:
    """Build a session that has already auto-stopped."""
    from dwf_mcp.streaming import RecordingSession
    rs = RecordingSession(
        record_id=sniff_id,
        task=None,
        notification_task=None,
        queue=asyncio.Queue(),
        chunks=[],
        lost_samples=0,
        done=True,
        error=None,
    )
    return _AsyncSniffSession(
        sniff_id=sniff_id,
        record_session=rs,
        allocator_key=allocator_key or f"sniff_i2c_{sniff_id}",
        started_at=0.0,
        completed_at=completed_at,
    )


def test_reap_evicts_old_completed_sessions() -> None:
    device = _FakeDevice()
    old = _make_completed_session(
        "old", completed_at=time.monotonic() - SNIFF_REAP_AFTER_S - 1
    )
    sessions = {"old": old}

    reap_completed_sessions(sessions, device)

    assert "old" not in sessions
    assert old.allocator_key in device.allocator.released


def test_reap_keeps_recent_completed_sessions() -> None:
    device = _FakeDevice()
    # Completed 10 seconds ago -- well within the 300s retention window
    recent = _make_completed_session(
        "recent", completed_at=time.monotonic() - 10.0
    )
    sessions = {"recent": recent}

    reap_completed_sessions(sessions, device)

    assert "recent" in sessions
    assert device.allocator.released == []


def test_reap_marks_done_session_with_completed_at_on_first_call() -> None:
    """A session whose record_session.done=True but completed_at=None should get
    completed_at set on the next reap sweep (but not yet evicted)."""
    device = _FakeDevice()
    session = _make_completed_session("freshly_done", completed_at=time.monotonic())
    session.completed_at = None  # simulate the just-finished state

    sessions = {"freshly_done": session}
    reap_completed_sessions(sessions, device)

    assert "freshly_done" in sessions  # not yet evicted
    assert session.completed_at is not None  # but timestamp now set


def test_reap_leaves_in_flight_sessions_alone() -> None:
    """Sessions whose record_session.done=False should not be touched."""
    from dwf_mcp.streaming import RecordingSession
    device = _FakeDevice()
    rs = RecordingSession(
        record_id="active",
        task=None,
        notification_task=None,
        queue=asyncio.Queue(),
        chunks=[],
        lost_samples=0,
        done=False,
        error=None,
    )
    session = _AsyncSniffSession(
        sniff_id="active",
        record_session=rs,
        allocator_key="sniff_i2c_active",
        started_at=time.monotonic(),
        completed_at=None,
    )
    sessions = {"active": session}

    reap_completed_sessions(sessions, device)

    assert "active" in sessions
    assert session.completed_at is None
    assert device.allocator.released == []
