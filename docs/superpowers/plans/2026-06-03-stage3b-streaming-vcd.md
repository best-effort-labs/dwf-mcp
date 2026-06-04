# Stage 3b Streaming + VCD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract shared streaming infrastructure to `streaming.py`, add `scope.record` tools, add MCP push notifications for streaming captures, and add `VcdStreamWriter` for bounded-memory incremental VCD output.

**Architecture:** New `streaming.py` module holds `RecordingSession`, `process_chunk`, `record_loop`, and `notification_loop`. `logic.py` is migrated to use it (no behavior change). `scope.py` gains `record_start/status/stop` and a `_mode` state machine. `vcd_writer.py` gains `VcdStreamWriter` for streaming VCD assembly. `server.py`'s `call_tool` gains `on_record_chunk` which injects an `on_chunk` callback into `record_start` calls so the MCP session can receive log-message notifications for each chunk.

**Tech Stack:** Python 3.12, asyncio, numpy, pydwf 1.1.x, pytest, pytest-asyncio, pyvcd (optional)

**Spec:** `docs/superpowers/specs/2026-06-03-stage3b-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/dwf_mcp/streaming.py` | Create | `RecordingSession`, `process_chunk`, `record_loop`, `notification_loop` |
| `src/dwf_mcp/instruments/logic.py` | Modify | Migrate to streaming.py; add VCD streaming via `on_chunk_sync` |
| `src/dwf_mcp/instruments/scope.py` | Modify | Add `_mode` state machine + `record_start/status/stop` |
| `src/dwf_mcp/backend.py` | Modify | Add `scope_record_*` stubs |
| `src/dwf_mcp/backends/fake.py` | Modify | Add FakeBackend for `scope_record_*` |
| `src/dwf_mcp/backends/pydwf_backend.py` | Modify | Implement `scope_record_*` against pydwf AnalogIn record mode |
| `src/dwf_mcp/vcd_writer.py` | Modify | Add `VcdStreamWriter` context-manager class |
| `src/dwf_mcp/device.py` | Modify | Add `vcd_enabled: bool` attribute |
| `src/dwf_mcp/server.py` | Modify | `call_tool` + `_make_instrument_handler` + `main` for MCP notifications; `build_app` for VCD toggle |
| `tests/unit/test_streaming.py` | Create | `RecordingSession`, `process_chunk`, `record_loop`, `notification_loop` |
| `tests/unit/test_scope_record.py` | Create | Scope record_start/status/stop + state machine |
| `tests/unit/test_logic.py` | Modify | Add VCD streaming path tests |
| `tests/unit/test_vcd_writer.py` | Modify | Add `VcdStreamWriter` tests |
| `tests/unit/test_server_async.py` | Modify | `on_record_chunk` injection tests |
| `tests/hardware/test_scope_record_hardware.py` | Create | Scope record hardware smoke tests |

---

## Task 1: streaming.py — new shared module

**Files:**
- Create: `src/dwf_mcp/streaming.py`
- Create: `tests/unit/test_streaming.py`

### Background

`logic.py` currently defines `_RecordingSession` (dataclass) and `_record_loop` (async method). This task extracts the generic pieces so scope can reuse them. The key design decisions from the spec:

- `asyncio.Queue(maxsize=32)` — poll loop uses `put_nowait`; full queue drops the notification silently (recording unaffected)
- Two tasks: `record_loop` (hardware poll) and `notification_loop` (MCP delivery) run independently so a slow client can't block hardware
- `process_chunk(session, chunk)`: if `on_chunk_sync` is set, call it (VCD write-through) and do NOT append to `session.chunks`; otherwise append. Used in both the poll loop and the drain path in `record_stop`.
- `on_chunk_sync` exceptions set `session.error` and stop the loop
- `notification_loop` exceptions are logged and swallowed — they never stop recording

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```
pytest tests/unit/test_streaming.py -v
```
Expected: `ModuleNotFoundError: No module named 'dwf_mcp.streaming'`

- [ ] **Step 3: Create `src/dwf_mcp/streaming.py`**

```python
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
    on_chunk_sync: Callable[[np.ndarray], None] | None = None
    # on_chunk_sync: called synchronously per chunk before queue put.
    # When set, chunks are NOT appended to session.chunks (write-through for VCD).
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)


def process_chunk(session: RecordingSession, chunk: np.ndarray) -> None:
    """Route chunk to on_chunk_sync (VCD write-through) or chunks list (npz accumulation)."""
    if session.on_chunk_sync is not None:
        session.on_chunk_sync(chunk)
    else:
        session.chunks.append(chunk)


async def record_loop(
    session: RecordingSession,
    poll_fn: Callable[[], tuple[int, int, int]],
    read_fn: Callable[[int], np.ndarray],
) -> None:
    """Hardware poll loop. Reads chunks, routes via process_chunk, and puts on queue.

    poll_fn() -> (available, lost, remaining)
    read_fn(n) -> np.ndarray of n samples
    """
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
    """MCP notification delivery loop. Consumes queue and calls on_chunk per item.

    Notification exceptions are logged and swallowed — they never stop recording.
    Exits when None sentinel is received.
    """
    while True:
        item = await session.queue.get()
        if item is None:
            break
        try:
            await on_chunk(session.record_id, item)
        except Exception:
            log.warning("notification send failed for record_id=%r", session.record_id)
```

- [ ] **Step 4: Run tests to confirm they pass**

```
pytest tests/unit/test_streaming.py -v
```
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/streaming.py tests/unit/test_streaming.py
git commit -m "feat: add shared streaming.py with RecordingSession, record_loop, notification_loop"
```

---

## Task 2: Migrate logic.py to streaming.py

**Files:**
- Modify: `src/dwf_mcp/instruments/logic.py`

### Background

`logic.py` currently defines `_RecordingSession` (a private dataclass) and `_record_loop` (an async method). Both are replaced by imports from `streaming.py`. The public API and FakeBackend test behavior are unchanged — existing tests should pass with no modification.

The key behavioral changes:
1. `_RecordingSession` → `RecordingSession` (same fields, same meaning)
2. `_record_loop` method → standalone `record_loop(session, poll_fn, read_fn)` function
3. `record_stop` must now cancel `session.notification_task` explicitly (not just `session.task`)
4. `release()` must also cancel notification tasks

The `on_chunk` parameter and VCD wiring are added in Task 6. In this task, `record_start` gains the `on_chunk` parameter and `session.meta` dict but VCD is not yet wired.

- [ ] **Step 1: Update `src/dwf_mcp/instruments/logic.py` — replace `_RecordingSession` and `_record_loop` with streaming imports**

Replace the entire file with:

```python
"""Logic (DigitalIn) instrument: buffer-mode capture and streaming record."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp import vcd_writer
from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.streaming import RecordingSession, notification_loop, process_chunk, record_loop

log = logging.getLogger(__name__)

_VALID_SOURCES = frozenset({"none", "detector_digital_in", "external1", "external2"})
_VALID_FORMATS = frozenset({"npz", "vcd"})


def _pins_to_mask(pins: list[str]) -> int:
    mask = 0
    for p in pins:
        mask |= 1 << int(p[3:])
    return mask


def _pin_indices(pins: list[str]) -> list[int]:
    return [int(p[3:]) for p in pins]


LOGIC_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pins", "sample_rate_hz", "buffer_size"],
    "properties": {
        "pins": {
            "type": "array",
            "items": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
            "minItems": 1,
            "uniqueItems": True,
        },
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "buffer_size": {"type": "integer", "minimum": 16, "maximum": 1_048_576},
    },
}

LOGIC_TRIGGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source"],
    "properties": {
        "source": {"type": "string", "enum": sorted(_VALID_SOURCES)},
        "pin": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
        "level": {"type": "number"},
        "condition": {"type": "string", "enum": ["Rising", "Falling", "Either"]},
        "position_s": {"type": "number", "default": 0.0},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}

LOGIC_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_path": {"type": "string"},
        "format": {"type": "string", "enum": ["npz", "vcd"], "default": "npz"},
    },
}

LOGIC_RECORD_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pins", "sample_rate_hz", "duration_s"],
    "properties": {
        "pins": {
            "type": "array",
            "items": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
            "minItems": 1,
            "uniqueItems": True,
        },
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "duration_s": {"type": "number", "minimum": 0.001},
        "output_path": {"type": "string"},
        "format": {"type": "string", "enum": ["npz", "vcd"], "default": "npz"},
    },
}

LOGIC_RECORD_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["record_id"],
    "properties": {"record_id": {"type": "string"}},
}


class Logic(Instrument):
    name = "logic"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":     ("configure",     LOGIC_CONFIGURE_SCHEMA),
        "set_trigger":   ("set_trigger",   LOGIC_TRIGGER_SCHEMA),
        "capture":       ("capture",       LOGIC_CAPTURE_SCHEMA),
        "record_start":  ("record_start",  LOGIC_RECORD_START_SCHEMA),
        "record_status": ("record_status", LOGIC_RECORD_ID_SCHEMA),
        "record_stop":   ("record_stop",   LOGIC_RECORD_ID_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None
        self._sessions: dict[str, RecordingSession] = {}

    # --- Buffer-mode ---

    def configure(
        self,
        pins: list[str],
        sample_rate_hz: float,
        buffer_size: int,
    ) -> dict[str, Any]:
        self.device.allocator.claim("logic", pins)
        self._config = None
        try:
            self.device.backend.logic_configure(
                pin_mask=_pins_to_mask(pins),
                sample_rate_hz=sample_rate_hz,
                buffer_size=buffer_size,
            )
        except Exception:
            self.device.allocator.release("logic")
            raise
        self._config = {
            "pins": list(pins),
            "sample_rate_hz": sample_rate_hz,
            "buffer_size": buffer_size,
        }
        return {"configured": True, "pins": pins}

    def set_trigger(
        self,
        source: str,
        pin: str | None = None,
        level: float | None = None,
        condition: str | None = None,
        position_s: float = 0.0,
        timeout_s: float = 1.0,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("logic.configure must be called before set_trigger")
        pin_idx = int(pin[3:]) if pin else None
        self.device.backend.logic_set_trigger(
            source=source,
            pin_idx=pin_idx,
            level=level,
            condition=condition,
            position_s=position_s,
            timeout_s=timeout_s,
        )
        return {"trigger_set": True}

    def capture(
        self,
        output_path: str | None = None,
        format: str = "npz",
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("logic.configure must be called before capture")
        if format not in _VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(_VALID_FORMATS)}, got {format!r}")
        if format == "vcd" and not self.device.vcd_enabled:
            raise ValueError(
                "VCD output is disabled (set DWF_ENABLE_VCD=1 or install dwf-mcp[vcd])"
            )
        cfg = self._config
        self.device.backend.logic_arm()
        deadline_s = max(cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0)
        import time
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            if self.device.backend.logic_status() == "Done":
                break
        else:
            raise RuntimeError("logic capture did not complete before deadline")

        raw = self.device.backend.logic_read(count=cfg["buffer_size"])
        pin_indices = _pin_indices(cfg["pins"])
        samples = raw[:, pin_indices].astype(np.uint8)

        return self._write_artifact(
            samples=samples,
            pin_names=cfg["pins"],
            sample_rate_hz=cfg["sample_rate_hz"],
            output_path=output_path,
            format=format,
        )

    def _write_artifact(
        self,
        samples: np.ndarray,
        pin_names: list[str],
        sample_rate_hz: float,
        output_path: str | None,
        format: str,
    ) -> dict[str, Any]:
        if format == "vcd":
            path = Path(output_path) if output_path else (
                self.artifacts.workspace / "captures" / f"logic_{uuid.uuid4().hex[:8]}.vcd"
            )
            vcd_writer.write(path, samples, pin_names, sample_rate_hz)
            return {"path": str(path), "format": "vcd", "n_samples": samples.shape[0]}

        arrays = {name: samples[:, i] for i, name in enumerate(pin_names)}
        summary = CaptureSummary(
            instrument="logic",
            sample_count=len(samples),
            sample_rate_hz=sample_rate_hz,
        )
        result = self.artifacts.write_npz(
            instrument="logic",
            arrays=arrays,
            config={"pins": pin_names, "sample_rate_hz": sample_rate_hz},
            summary=summary,
            output_path=Path(output_path) if output_path else None,
        )
        return {
            "path": result.path,
            "sidecar_path": result.sidecar_path,
            "format": "npz",
            "n_samples": samples.shape[0],
        }

    # --- Streaming (record) ---

    async def record_start(
        self,
        pins: list[str],
        sample_rate_hz: float,
        duration_s: float,
        output_path: str | None = None,
        format: str = "npz",
        on_chunk: Callable[[str, np.ndarray], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if format not in _VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(_VALID_FORMATS)}, got {format!r}")
        if format == "vcd" and not self.device.vcd_enabled:
            raise ValueError(
                "VCD output is disabled (set DWF_ENABLE_VCD=1 or install dwf-mcp[vcd])"
            )
        self.device.allocator.claim("logic", pins)
        try:
            self.device.backend.logic_record_configure(
                pin_mask=_pins_to_mask(pins),
                sample_rate_hz=sample_rate_hz,
                duration_s=duration_s,
            )
            self.device.backend.logic_record_arm()
        except Exception:
            self.device.allocator.release("logic")
            raise

        record_id = str(uuid.uuid4())
        queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue(maxsize=32)
        session = RecordingSession(
            record_id=record_id,
            task=None,
            notification_task=None,
            queue=queue,
            chunks=[],
            lost_samples=0,
            done=False,
            error=None,
            on_chunk=on_chunk,
            on_chunk_sync=None,  # VCD wired in Task 6
            meta={
                "pins": list(pins),
                "sample_rate_hz": sample_rate_hz,
                "output_path": output_path,
                "format": format,
                "vcd_writer": None,
                "vcd_path": None,
            },
        )
        try:
            session.task = asyncio.create_task(
                record_loop(
                    session,
                    self.device.backend.logic_record_status,
                    self.device.backend.logic_record_read,
                )
            )
            if on_chunk is not None:
                session.notification_task = asyncio.create_task(
                    notification_loop(session, on_chunk)
                )
        except Exception:
            if session.task is not None:
                session.task.cancel()
            try:
                self.device.backend.logic_record_stop()
            except Exception:
                pass
            self.device.allocator.release("logic")
            raise

        self._sessions[record_id] = session
        return {"record_id": record_id}

    def record_status(self, record_id: str) -> dict[str, Any]:
        session = self._sessions.get(record_id)
        if session is None:
            raise ValueError(f"unknown record_id {record_id!r}")
        return {
            "record_id": record_id,
            "done": session.done,
            "chunks_received": len(session.chunks),
            "lost_samples": session.lost_samples,
            "error": session.error,
        }

    async def record_stop(self, record_id: str) -> dict[str, Any]:
        session = self._sessions.get(record_id)
        if session is None:
            raise ValueError(f"unknown record_id {record_id!r}")
        try:
            # 1. Cancel background tasks.
            if session.task is not None:
                session.task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.task
            if session.notification_task is not None:
                session.notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.notification_task
            # 2. Stop hardware acquisition.
            try:
                self.device.backend.logic_record_stop()
            except Exception as exc:
                log.warning("logic_record_stop failed: %s", exc)
            # 3. Drain any remaining available samples.
            try:
                available, lost, _ = self.device.backend.logic_record_status()
                session.lost_samples += lost
                if available > 0:
                    chunk = self.device.backend.logic_record_read(available)
                    try:
                        process_chunk(session, chunk)
                    except Exception as exc:
                        log.warning("drain process_chunk failed: %s", exc)
            except Exception as exc:
                log.warning("drain after logic_record_stop failed: %s", exc)
            # 4. Write artifact (best-effort).
            artifact_path: str | None = None
            artifact_error: str | None = None
            fmt = session.meta.get("format", "npz")
            if fmt == "vcd":
                artifact_path = session.meta.get("vcd_path")
            elif session.chunks:
                try:
                    pins = session.meta["pins"]
                    pin_indices = _pin_indices(pins)
                    all_raw = np.concatenate(session.chunks, axis=0)
                    samples = all_raw[:, pin_indices].astype(np.uint8)
                    result_dict = self._write_artifact(
                        samples=samples,
                        pin_names=pins,
                        sample_rate_hz=session.meta["sample_rate_hz"],
                        output_path=session.meta.get("output_path"),
                        format="npz",
                    )
                    artifact_path = result_dict.get("path")
                except Exception as exc:
                    log.exception("artifact write failed for record_id=%r", record_id)
                    artifact_error = str(exc)
        finally:
            # Close VCD writer if present (Task 6 sets this).
            vcd_w = session.meta.get("vcd_writer")
            if vcd_w is not None:
                with suppress(Exception):
                    vcd_w.close()
            del self._sessions[record_id]
            self.device.allocator.release("logic")

        return {
            "record_id": record_id,
            "artifact_path": artifact_path,
            "lost_samples": session.lost_samples,
            "error": session.error,
            "artifact_error": artifact_error,
        }

    def release(self) -> None:
        for session in list(self._sessions.values()):
            if session.task is not None:
                session.task.cancel()
            if session.notification_task is not None:
                session.notification_task.cancel()
        self._sessions.clear()
        self.device.allocator.release("logic")
        self._config = None
```

- [ ] **Step 2: Add `vcd_enabled` stub to DwfDevice so the import doesn't break**

In `src/dwf_mcp/device.py`, add `self.vcd_enabled: bool = True` to `__init__` after the existing attribute assignments:

```python
    def __init__(
        self,
        backend: DwfBackend,
        policy: SafetyPolicy,
        allocator: PinAllocator,
        workspace: Path | str,
        idle_timeout_s: float = 600.0,
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.allocator = allocator
        self.workspace = workspace
        self.idle_timeout_s = idle_timeout_s
        self.vcd_enabled: bool = True   # add this line
        self._info: DeviceInfo | None = None
        self._last_activity: float | None = None
        self._serial_request: str | None = None
```

- [ ] **Step 3: Run existing logic tests to confirm they still pass**

```
pytest tests/unit/test_logic.py -v
```
Expected: all existing tests pass (no count change yet)

- [ ] **Step 4: Commit**

```bash
git add src/dwf_mcp/instruments/logic.py src/dwf_mcp/device.py
git commit -m "refactor: migrate logic.py to use shared streaming.py; add vcd_enabled to DwfDevice"
```

---

## Task 3: Backend stubs + FakeBackend for scope record

**Files:**
- Modify: `src/dwf_mcp/backend.py`
- Modify: `src/dwf_mcp/backends/fake.py`

- [ ] **Step 1: Add `scope_record_*` stubs to `backend.py`**

After the `# Scope (AnalogIn)` block (after `scope_read`), append:

```python
    # Scope record-mode (AnalogIn streaming) — added in stage 3b.
    def scope_record_configure(
        self,
        channels: list[int],
        range_v: float,
        offset_v: float,
        coupling: str,
        sample_rate_hz: float,
        duration_s: float,
    ) -> None:
        raise NotImplementedError

    def scope_record_arm(self) -> None:
        raise NotImplementedError

    def scope_record_status(self) -> tuple[int, int, int]:
        raise NotImplementedError

    def scope_record_read(self, count: int) -> np.ndarray:
        raise NotImplementedError

    def scope_record_stop(self) -> None:
        raise NotImplementedError
```

- [ ] **Step 2: Add scope record state + methods to `fake.py`**

In `FakeBackend.__init__`, after the `# Logic record-mode state` block, add:

```python
        # Scope record-mode state
        self.scope_record_calls: list[tuple[str, dict[str, Any]]] = []
        self._scope_record_status_sequence: list[tuple[int, int, int]] = [(10, 0, 0)]
        self._scope_record_status_idx = 0
        self._scope_record_canned_chunk: np.ndarray = np.zeros((10, 2), dtype=np.float64)
```

Then add methods at the end of `FakeBackend` (before the closing of the class):

```python
    # --- Scope record-mode ---

    def scope_record_configure(
        self,
        channels: list[int],
        range_v: float,
        offset_v: float,
        coupling: str,
        sample_rate_hz: float,
        duration_s: float,
    ) -> None:
        self.scope_record_calls.append(("scope_record_configure", {
            "channels": channels, "range_v": range_v, "offset_v": offset_v,
            "coupling": coupling, "sample_rate_hz": sample_rate_hz,
            "duration_s": duration_s,
        }))

    def scope_record_arm(self) -> None:
        self.scope_record_calls.append(("scope_record_arm", {}))

    def scope_record_status(self) -> tuple[int, int, int]:
        self.scope_record_calls.append(("scope_record_status", {}))
        idx = self._scope_record_status_idx
        seq = self._scope_record_status_sequence
        result = seq[min(idx, len(seq) - 1)]
        self._scope_record_status_idx += 1
        return result

    def scope_record_read(self, count: int) -> np.ndarray:
        self.scope_record_calls.append(("scope_record_read", {"count": count}))
        return self._scope_record_canned_chunk[:count].copy()

    def scope_record_stop(self) -> None:
        self.scope_record_calls.append(("scope_record_stop", {}))
```

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

```
pytest tests/unit/ -v
```
Expected: all existing tests pass

- [ ] **Step 4: Commit**

```bash
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/fake.py
git commit -m "feat: add scope_record_* stubs to DwfBackend and FakeBackend implementations"
```

---

## Task 4: Scope record tools + state machine

**Files:**
- Modify: `src/dwf_mcp/instruments/scope.py`
- Create: `tests/unit/test_scope_record.py`

### Background

`Scope` gains three tools (`record_start/status/stop`) and a `_mode: Literal[None, "buffer", "record"]` state machine to enforce mutual exclusion between buffer and record modes. The allocator alone cannot prevent the same instrument instance from entering both modes — the state machine is required.

Valid transitions:
- `None` → `configure()` → `"buffer"` ✓
- `None` → `record_start()` → `"record"` ✓
- `"buffer"` → `configure()` → `"buffer"` ✓ (replace config)
- `"buffer"` → `record_start()` → `"record"` ✓ (implicit buffer release)
- `"record"` → `configure()` → **raises `RuntimeError`**
- `"record"` → `record_start()` → **raises `RuntimeError`**

`record_start()` when `_mode == "buffer"` implicitly releases: calls `allocator.release("scope")`, resets `_config = None`, `_mode = None`, then proceeds with record setup.

- [ ] **Step 1: Write failing tests in `tests/unit/test_scope_record.py`**

```python
"""Tests for Scope record_start/status/stop and _mode state machine."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.policy import SafetyPolicy


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def scope(device: DwfDevice, tmp_path: Path) -> Scope:
    device.open()
    return Scope(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


@pytest.mark.asyncio
async def test_record_start_returns_record_id(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert "record_id" in result
    assert isinstance(result["record_id"], str)


@pytest.mark.asyncio
async def test_record_start_sets_mode_to_record(scope: Scope) -> None:
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert scope._mode == "record"


@pytest.mark.asyncio
async def test_record_start_claims_scope_pins(scope: Scope) -> None:
    await scope.record_start(
        channels=[1, 2], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    claimed = scope.device.allocator.claimed_pins()
    assert "scope1" in claimed and "scope2" in claimed


@pytest.mark.asyncio
async def test_configure_while_in_record_mode_raises(scope: Scope) -> None:
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    with pytest.raises(RuntimeError, match="record mode"):
        scope.configure(channels=[1], range_v=5.0, sample_rate_hz=10_000.0, buffer_size=1024)


@pytest.mark.asyncio
async def test_record_start_while_in_record_mode_raises(scope: Scope) -> None:
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    with pytest.raises(RuntimeError, match="record mode"):
        await scope.record_start(
            channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
        )


def test_configure_sets_mode_to_buffer(scope: Scope) -> None:
    scope.configure(channels=[1], range_v=5.0, sample_rate_hz=10_000.0, buffer_size=1024)
    assert scope._mode == "buffer"


@pytest.mark.asyncio
async def test_record_start_while_in_buffer_mode_releases_buffer(scope: Scope) -> None:
    scope.configure(channels=[1], range_v=5.0, sample_rate_hz=10_000.0, buffer_size=1024)
    assert scope._mode == "buffer"
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert scope._mode == "record"
    assert scope._config is None


@pytest.mark.asyncio
async def test_record_status_returns_fields(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    status = scope.record_status(record_id)
    assert status["record_id"] == record_id
    assert "done" in status
    assert "lost_samples" in status


@pytest.mark.asyncio
async def test_record_stop_writes_npz_artifact(scope: Scope, tmp_path: Path) -> None:
    fake: FakeBackend = scope.device.backend  # type: ignore[assignment]
    fake._scope_record_canned_chunk = np.random.rand(10, 2).astype(np.float64)
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    stop = await scope.record_stop(record_id)
    assert stop["artifact_error"] is None
    assert stop["artifact_path"] is not None
    assert Path(stop["artifact_path"]).exists()


@pytest.mark.asyncio
async def test_record_stop_releases_pins(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    await scope.record_stop(record_id)
    claimed = scope.device.allocator.claimed_pins()
    assert "scope1" not in claimed


@pytest.mark.asyncio
async def test_record_stop_resets_mode_to_none(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    await scope.record_stop(record_id)
    assert scope._mode is None


@pytest.mark.asyncio
async def test_record_stop_unknown_id_raises(scope: Scope) -> None:
    with pytest.raises(ValueError, match="unknown record_id"):
        await scope.record_stop("no-such-id")


@pytest.mark.asyncio
async def test_record_after_stop_is_possible(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    await asyncio.sleep(0.05)
    await scope.record_stop(result["record_id"])
    # Can start another record after stopping
    result2 = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert "record_id" in result2
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/unit/test_scope_record.py -v
```
Expected: `AttributeError: 'Scope' object has no attribute '_mode'` or similar

- [ ] **Step 3: Update `src/dwf_mcp/instruments/scope.py`**

Replace the entire file with:

```python
"""Scope (analog-in) instrument. Buffer-mode and streaming record-mode acquisition."""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.streaming import RecordingSession, notification_loop, process_chunk, record_loop

import logging
log = logging.getLogger(__name__)

_VALID_COUPLINGS = {"DC", "AC"}
_VALID_CONDITIONS = {"Rising", "Falling", "Either"}

SCOPE_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channels", "range_v", "sample_rate_hz", "buffer_size"],
    "properties": {
        "channels": {
            "type": "array",
            "items": {"type": "integer", "enum": [1, 2]},
            "minItems": 1,
            "uniqueItems": True,
        },
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0},
        "offset_v": {"type": "number", "default": 0.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "buffer_size": {"type": "integer", "minimum": 16, "maximum": 1_048_576},
    },
}

SCOPE_TRIGGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source"],
    "properties": {
        "source": {
            "type": "string",
            "enum": ["none", "detector_analog_in", "external1", "external2"],
        },
        "channel": {"type": "integer", "enum": [1, 2]},
        "level_v": {"type": "number", "default": 0.0},
        "condition": {
            "type": "string",
            "enum": ["Rising", "Falling", "Either"],
            "default": "Rising",
        },
        "position_s": {"type": "number", "default": 0.0},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}

SCOPE_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_path": {"type": "string"},
        "description": {"type": "string"},
    },
}

SCOPE_RECORD_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channels", "range_v", "sample_rate_hz", "duration_s"],
    "properties": {
        "channels": {
            "type": "array",
            "items": {"type": "integer", "enum": [1, 2]},
            "minItems": 1,
            "uniqueItems": True,
        },
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0},
        "offset_v": {"type": "number", "default": 0.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "duration_s": {"type": "number", "minimum": 0.001},
        "output_path": {"type": "string"},
    },
}

SCOPE_RECORD_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["record_id"],
    "properties": {"record_id": {"type": "string"}},
}


class Scope(Instrument):
    name = "scope"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":      ("configure",      SCOPE_CONFIGURE_SCHEMA),
        "set_trigger":    ("set_trigger",    SCOPE_TRIGGER_SCHEMA),
        "capture":        ("capture",        SCOPE_CAPTURE_SCHEMA),
        "record_start":   ("record_start",   SCOPE_RECORD_START_SCHEMA),
        "record_status":  ("record_status",  SCOPE_RECORD_ID_SCHEMA),
        "record_stop":    ("record_stop",    SCOPE_RECORD_ID_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None
        self._trigger: dict[str, Any] | None = None
        self._mode: Literal[None, "buffer", "record"] = None
        self._sessions: dict[str, RecordingSession] = {}

    # --- Buffer-mode ---

    def configure(
        self,
        channels: list[int],
        range_v: float,
        sample_rate_hz: float,
        buffer_size: int,
        offset_v: float = 0.0,
        coupling: str = "DC",
    ) -> dict[str, Any]:
        if self._mode == "record":
            raise RuntimeError(
                "scope is in record mode — call scope.record_stop() before reconfiguring"
            )
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(
                f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}"
            )
        pin_names = [f"scope{c}" for c in channels]
        self.device.allocator.claim("scope", pin_names)
        self._config = None
        self._trigger = None
        try:
            for ch in (1, 2):
                self.device.backend.scope_configure(
                    channel=ch,
                    range_v=range_v,
                    offset_v=offset_v,
                    coupling=coupling,
                    enable=(ch in channels),
                )
            self.device.backend.scope_set_acquisition(
                sample_rate_hz=sample_rate_hz,
                buffer_size=buffer_size,
                mode="Single",
            )
        except Exception:
            self.device.allocator.release("scope")
            raise
        self._config = {
            "channels": list(channels),
            "range_v": range_v,
            "offset_v": offset_v,
            "coupling": coupling,
            "sample_rate_hz": sample_rate_hz,
            "buffer_size": buffer_size,
        }
        self._mode = "buffer"
        return {"configured": True}

    def set_trigger(
        self,
        source: str,
        channel: int | None = None,
        level_v: float = 0.0,
        condition: str = "Rising",
        position_s: float = 0.0,
        timeout_s: float = 1.0,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured(
                "scope.configure must be called before set_trigger"
            )
        if condition not in _VALID_CONDITIONS:
            raise ValueError(
                f"condition must be one of {sorted(_VALID_CONDITIONS)}, got {condition!r}"
            )
        self.device.backend.scope_set_trigger(
            source=source,
            channel=channel,
            level_v=level_v,
            condition=condition,
            position_s=position_s,
            timeout_s=timeout_s,
        )
        self._trigger = {
            "source": source,
            "channel": channel,
            "level_v": level_v,
            "condition": condition,
            "position_s": position_s,
            "timeout_s": timeout_s,
        }
        return {"trigger_set": True}

    def capture(
        self,
        output_path: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        if self._config is None or self._mode != "buffer":
            raise InstrumentNotConfigured(
                "scope.configure must be called before capture"
            )
        cfg = self._config
        self.device.backend.scope_arm()
        deadline = time.monotonic() + max(
            cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0
        )
        while time.monotonic() < deadline:
            if self.device.backend.scope_status() == "Done":
                break
        else:
            raise RuntimeError("scope capture did not complete before deadline")

        arrays: dict[str, np.ndarray[Any, Any]] = {}
        summary_per_ch: dict[str, dict[str, float]] = {}
        for ch in cfg["channels"]:
            samples = self.device.backend.scope_read(
                channel=ch, count=cfg["buffer_size"]
            )
            arrays[f"ch{ch}"] = samples
            summary_per_ch[f"ch{ch}"] = self._summarize(samples, cfg["sample_rate_hz"])

        summary = CaptureSummary(
            instrument="scope",
            sample_count=cfg["buffer_size"],
            sample_rate_hz=cfg["sample_rate_hz"],
            extra=summary_per_ch,
        )
        sidecar_config = {**cfg, "trigger": self._trigger}
        result = self.artifacts.write_npz(
            instrument="scope",
            arrays=arrays,
            config=sidecar_config,
            summary=summary,
            output_path=Path(output_path) if output_path else None,
            description=description,
        )
        return {
            "path": result.path,
            "sidecar_path": result.sidecar_path,
            "summary": summary_per_ch,
        }

    # --- Record-mode ---

    async def record_start(
        self,
        channels: list[int],
        range_v: float,
        sample_rate_hz: float,
        duration_s: float,
        offset_v: float = 0.0,
        coupling: str = "DC",
        output_path: str | None = None,
        on_chunk: Callable[[str, np.ndarray], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if self._mode == "record":
            raise RuntimeError(
                "scope is already in record mode — call scope.record_stop() first"
            )
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(
                f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}"
            )
        # Implicit buffer release when switching from buffer to record mode.
        if self._mode == "buffer":
            self.device.allocator.release("scope")
            self._config = None
            self._trigger = None
            self._mode = None

        pin_names = [f"scope{c}" for c in channels]
        self.device.allocator.claim("scope", pin_names)
        try:
            self.device.backend.scope_record_configure(
                channels=list(channels),
                range_v=range_v,
                offset_v=offset_v,
                coupling=coupling,
                sample_rate_hz=sample_rate_hz,
                duration_s=duration_s,
            )
            self.device.backend.scope_record_arm()
        except Exception:
            self.device.allocator.release("scope")
            raise

        record_id = str(uuid.uuid4())
        queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue(maxsize=32)
        session = RecordingSession(
            record_id=record_id,
            task=None,
            notification_task=None,
            queue=queue,
            chunks=[],
            lost_samples=0,
            done=False,
            error=None,
            on_chunk=on_chunk,
            on_chunk_sync=None,
            meta={
                "channels": list(channels),
                "range_v": range_v,
                "sample_rate_hz": sample_rate_hz,
                "output_path": output_path,
            },
        )
        try:
            session.task = asyncio.create_task(
                record_loop(
                    session,
                    self.device.backend.scope_record_status,
                    self.device.backend.scope_record_read,
                )
            )
            if on_chunk is not None:
                session.notification_task = asyncio.create_task(
                    notification_loop(session, on_chunk)
                )
        except Exception:
            if session.task is not None:
                session.task.cancel()
            try:
                self.device.backend.scope_record_stop()
            except Exception:
                pass
            self.device.allocator.release("scope")
            raise

        self._sessions[record_id] = session
        self._mode = "record"
        return {"record_id": record_id}

    def record_status(self, record_id: str) -> dict[str, Any]:
        session = self._sessions.get(record_id)
        if session is None:
            raise ValueError(f"unknown record_id {record_id!r}")
        return {
            "record_id": record_id,
            "done": session.done,
            "chunks_received": len(session.chunks),
            "lost_samples": session.lost_samples,
            "error": session.error,
        }

    async def record_stop(self, record_id: str) -> dict[str, Any]:
        session = self._sessions.get(record_id)
        if session is None:
            raise ValueError(f"unknown record_id {record_id!r}")
        artifact_path: str | None = None
        sidecar_path: str | None = None
        artifact_error: str | None = None
        try:
            # 1. Cancel background tasks.
            if session.task is not None:
                session.task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.task
            if session.notification_task is not None:
                session.notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.notification_task
            # 2. Stop hardware.
            try:
                self.device.backend.scope_record_stop()
            except Exception as exc:
                log.warning("scope_record_stop failed: %s", exc)
            # 3. Drain remaining samples.
            try:
                available, lost, _ = self.device.backend.scope_record_status()
                session.lost_samples += lost
                if available > 0:
                    chunk = self.device.backend.scope_record_read(available)
                    try:
                        process_chunk(session, chunk)
                    except Exception as exc:
                        log.warning("drain process_chunk failed: %s", exc)
            except Exception as exc:
                log.warning("drain after scope_record_stop failed: %s", exc)
            # 4. Write npz artifact (best-effort).
            if session.chunks:
                try:
                    channels = session.meta["channels"]
                    all_raw = np.concatenate(session.chunks, axis=0)
                    # scope_record_read always returns shape (N, 2); slice to configured channels.
                    ch_indices = [c - 1 for c in channels]
                    arrays = {f"ch{c}": all_raw[:, i] for i, c in zip(ch_indices, channels)}
                    summary_per_ch = {
                        f"ch{c}": self._summarize(
                            all_raw[:, i], session.meta["sample_rate_hz"]
                        )
                        for i, c in zip(ch_indices, channels)
                    }
                    summary = CaptureSummary(
                        instrument="scope",
                        sample_count=all_raw.shape[0],
                        sample_rate_hz=session.meta["sample_rate_hz"],
                        extra=summary_per_ch,
                    )
                    out = session.meta.get("output_path")
                    npz_result = self.artifacts.write_npz(
                        instrument="scope",
                        arrays=arrays,
                        config=session.meta,
                        summary=summary,
                        output_path=Path(out) if out else None,
                    )
                    artifact_path = npz_result.path
                    sidecar_path = npz_result.sidecar_path
                except Exception as exc:
                    log.exception("artifact write failed for record_id=%r", record_id)
                    artifact_error = str(exc)
        finally:
            del self._sessions[record_id]
            self.device.allocator.release("scope")
            self._mode = None

        return {
            "record_id": record_id,
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "lost_samples": session.lost_samples,
            "error": session.error,
            "artifact_error": artifact_error,
        }

    def release(self) -> None:
        for session in list(self._sessions.values()):
            if session.task is not None:
                session.task.cancel()
            if session.notification_task is not None:
                session.notification_task.cancel()
        self._sessions.clear()
        self.device.allocator.release("scope")
        self._config = None
        self._trigger = None
        self._mode = None

    @staticmethod
    def _summarize(
        samples: np.ndarray[Any, Any], sample_rate_hz: float
    ) -> dict[str, float]:
        arr = np.asarray(samples, dtype=np.float64)
        if len(arr) == 0:
            return {
                "min": 0.0, "max": 0.0, "mean": 0.0, "rms": 0.0,
                "freq_estimate": 0.0, "sample_rate": sample_rate_hz,
            }
        mean = float(arr.mean())
        rms = float(np.sqrt(np.mean(arr**2)))
        if len(arr) > 0:
            midpoint = float(arr.max() + arr.min()) / 2.0
            centered = arr - midpoint
            signs = np.signbit(centered)
            crossings = int(np.sum(signs[:-1] != signs[1:]))
            freq_estimate = (crossings / 2.0) * (sample_rate_hz / len(arr))
        else:
            freq_estimate = 0.0
        return {
            "min": float(arr.min()),
            "max": float(arr.max()),
            "mean": mean,
            "rms": rms,
            "freq_estimate": freq_estimate,
            "sample_rate": sample_rate_hz,
        }
```

- [ ] **Step 4: Register scope record tools in `server.py`**

In `server.py`, `build_app()` already calls `app.register_instrument(Scope)`. The three new tools (`scope.record_start`, `scope.record_status`, `scope.record_stop`) are declared in `Scope.tools` so they are registered automatically — no change needed.

- [ ] **Step 5: Run scope record tests**

```
pytest tests/unit/test_scope_record.py -v
```
Expected: all 13 tests pass

- [ ] **Step 6: Run full unit suite to verify no regressions**

```
pytest tests/unit/ -v
```
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/scope.py tests/unit/test_scope_record.py
git commit -m "feat: add scope.record_start/status/stop with _mode state machine"
```

---

## Task 5: VcdStreamWriter + runtime config toggle

**Files:**
- Modify: `src/dwf_mcp/vcd_writer.py`
- Modify: `src/dwf_mcp/device.py` (already has `vcd_enabled` from Task 2)
- Modify: `src/dwf_mcp/server.py` (build_app)
- Modify: `tests/unit/test_vcd_writer.py`

### Background

`VcdStreamWriter` is a context-manager that holds a pyvcd `VCDWriter` open across multiple `write_chunk()` calls. A running sample counter tracks the cumulative position so timestamps are correct across chunk boundaries. The existing one-shot `write()` function is unchanged.

The `enable_vcd` param in `build_app` and `DWF_ENABLE_VCD` env var allow runtime disabling regardless of pyvcd install state, and explicit enabling that fails fast at startup if pyvcd is missing.

- [ ] **Step 1: Add `VcdStreamWriter` tests to `tests/unit/test_vcd_writer.py`**

Append to the existing file:

```python

def test_vcd_stream_writer_single_chunk_matches_oneshot(tmp_path: Path) -> None:
    """VcdStreamWriter with one full chunk produces identical output to write()."""
    from dwf_mcp.vcd_writer import VcdStreamWriter, write as vcd_write

    samples = np.array([[0, 0], [0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.uint8)
    pin_names = ["dio0", "dio1"]
    rate = 1_000_000.0

    path_oneshot = tmp_path / "oneshot.vcd"
    vcd_write(path_oneshot, samples, pin_names, rate)

    path_stream = tmp_path / "stream.vcd"
    with VcdStreamWriter(path_stream, pin_names, rate) as w:
        w.write_chunk(samples)

    assert path_stream.read_text() == path_oneshot.read_text()


def test_vcd_stream_writer_multi_chunk_matches_oneshot(tmp_path: Path) -> None:
    """VcdStreamWriter split across two chunks produces same output as write() on full array."""
    from dwf_mcp.vcd_writer import VcdStreamWriter, write as vcd_write

    samples = np.array(
        [[1, 0], [1, 1], [0, 1], [0, 0], [1, 0], [0, 0]], dtype=np.uint8
    )
    pin_names = ["dio0", "dio1"]
    rate = 1_000_000.0

    path_oneshot = tmp_path / "oneshot.vcd"
    vcd_write(path_oneshot, samples, pin_names, rate)

    path_stream = tmp_path / "stream.vcd"
    with VcdStreamWriter(path_stream, pin_names, rate) as w:
        w.write_chunk(samples[:3])
        w.write_chunk(samples[3:])

    assert path_stream.read_text() == path_oneshot.read_text()


def test_vcd_stream_writer_sample_counter_advances(tmp_path: Path) -> None:
    """Sample counter correctly advances so chunk-boundary timestamps are correct."""
    from dwf_mcp.vcd_writer import VcdStreamWriter

    # Chunk 1: no transitions at t=0,1,2
    chunk1 = np.array([[0, 0], [0, 0], [0, 0]], dtype=np.uint8)
    # Chunk 2: transition at t=3 (index 0 of chunk2 → global t=3)
    chunk2 = np.array([[1, 0], [1, 0]], dtype=np.uint8)

    path = tmp_path / "counter.vcd"
    with VcdStreamWriter(path, ["dio0", "dio1"], 1_000_000.0) as w:
        w.write_chunk(chunk1)
        w.write_chunk(chunk2)

    content = path.read_text()
    assert "#3" in content  # transition at global t=3


def test_vcd_stream_writer_close_is_idempotent(tmp_path: Path) -> None:
    from dwf_mcp.vcd_writer import VcdStreamWriter

    path = tmp_path / "idem.vcd"
    w = VcdStreamWriter(path, ["dio0"], 1_000_000.0)
    w.write_chunk(np.array([[0], [1]], dtype=np.uint8))
    w.close()
    w.close()  # second close must not raise


def test_vcd_stream_writer_missing_package_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dwf_mcp.vcd_writer as vw
    monkeypatch.setattr(vw, "HAS_VCD", False)
    with pytest.raises(ImportError, match="pyvcd"):
        vw.VcdStreamWriter(tmp_path / "out.vcd", ["a"], 1_000_000.0)
```

- [ ] **Step 2: Run tests to confirm they fail**

```
pytest tests/unit/test_vcd_writer.py -v
```
Expected: new tests fail with `ImportError` or `AttributeError: module 'dwf_mcp.vcd_writer' has no attribute 'VcdStreamWriter'`

- [ ] **Step 3: Update `src/dwf_mcp/vcd_writer.py` — add `_compute_timescale` helper and `VcdStreamWriter`**

Replace the entire file with:

```python
"""Thin wrapper around pyvcd for writing VCD logic capture files.

pyvcd PyPI package (pip install pyvcd) imports as `vcd`.
Optional: only used when logic format="vcd" is requested.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

try:
    import vcd as _vcd  # installed as pyvcd
    HAS_VCD = True
except ImportError:
    HAS_VCD = False


def _compute_timescale(sample_rate_hz: float) -> tuple[str, int]:
    """Return (timescale_str, time_scale_factor) for the given sample rate."""
    period_s = 1.0 / sample_rate_hz
    if period_s < 1e-9:
        return "1 ps", int(round(period_s * 1e12))
    if period_s < 1e-6:
        return "1 ns", int(round(period_s * 1e9))
    if period_s < 1e-3:
        return "1 us", int(round(period_s * 1e6))
    return "1 ms", int(round(period_s * 1e3))


def write(
    path: Path,
    samples: np.ndarray,
    pin_names: list[str],
    sample_rate_hz: float,
) -> None:
    """Write samples (uint8, shape (n_samples, n_pins)) to a VCD file."""
    if not HAS_VCD:
        raise ImportError(
            "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
        )

    n_samples, n_pins = samples.shape
    if len(pin_names) != n_pins:
        raise ValueError(
            f"pin_names length {len(pin_names)} does not match samples columns {n_pins}"
        )
    timescale, time_scale_factor = _compute_timescale(sample_rate_hz)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as f, _vcd.VCDWriter(f, timescale=timescale, date="today") as writer:
        vars_: list[Any] = [
            writer.register_var("logic", name, "wire", size=1)
            for name in pin_names
        ]
        for i, var in enumerate(vars_):
            writer.change(var, 0, int(samples[0, i]))

        prev = samples[0].copy()
        for sample_idx in range(1, n_samples):
            t = sample_idx * time_scale_factor
            row = samples[sample_idx]
            for pin_idx in range(n_pins):
                if row[pin_idx] != prev[pin_idx]:
                    writer.change(vars_[pin_idx], t, int(row[pin_idx]))
            prev = row.copy()


class VcdStreamWriter:
    """Incremental VCD writer for streaming digital capture.

    Opens the output file in __init__ and appends transitions chunk by chunk.
    Call close() (or use as a context manager) to finalize.

    Raises ImportError at construction time if pyvcd is not installed.
    """

    def __init__(self, path: Path, pin_names: list[str], sample_rate_hz: float) -> None:
        if not HAS_VCD:
            raise ImportError(
                "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
            )
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        timescale, self._time_scale_factor = _compute_timescale(sample_rate_hz)
        self._f = self._path.open("w")
        self._writer = _vcd.VCDWriter(self._f, timescale=timescale, date="today")
        self._vars: list[Any] = [
            self._writer.register_var("logic", name, "wire", size=1)
            for name in pin_names
        ]
        self._n_pins = len(pin_names)
        self._sample_counter = 0
        self._prev: np.ndarray | None = None
        self._closed = False

    def write_chunk(self, chunk: np.ndarray) -> None:
        """Append transitions from chunk (uint8, shape (N, n_pins)) to the open VCD file."""
        n_samples = chunk.shape[0]
        start_idx = self._sample_counter
        for i in range(n_samples):
            t = (start_idx + i) * self._time_scale_factor
            row = chunk[i]
            if self._prev is None:
                for pin_idx, var in enumerate(self._vars):
                    self._writer.change(var, 0, int(row[pin_idx]))
            else:
                for pin_idx in range(self._n_pins):
                    if row[pin_idx] != self._prev[pin_idx]:
                        self._writer.change(self._vars[pin_idx], t, int(row[pin_idx]))
            self._prev = row.copy()
        self._sample_counter += n_samples

    def close(self) -> None:
        """Finalize and close the VCD file. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        finally:
            self._f.close()

    def __enter__(self) -> VcdStreamWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
```

- [ ] **Step 4: Update `build_app` in `server.py` to accept `enable_vcd`**

In `src/dwf_mcp/server.py`, update `build_app`:

```python
def build_app(
    backend_name: str | None = None,
    workspace: str | None = None,
    idle_timeout_s: float = 600.0,
    enable_vcd: bool | None = None,
) -> DwfMcpApp:
    backend_name = backend_name or os.environ.get("DWF_BACKEND", "pydwf")
    backend = _build_backend(backend_name)
    allocator = PinAllocator(resource_groups=AD3_RESOURCE_GROUPS)
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=allocator,
        workspace=workspace or "",
        idle_timeout_s=idle_timeout_s,
    )
    if enable_vcd is True:
        from dwf_mcp import vcd_writer as _vw
        if not _vw.HAS_VCD:
            raise ImportError(
                "enable_vcd=True but pyvcd is not installed: pip install dwf-mcp[vcd]"
            )
        device.vcd_enabled = True
    elif enable_vcd is False:
        device.vcd_enabled = False
    else:
        from dwf_mcp import vcd_writer as _vw
        device.vcd_enabled = _vw.HAS_VCD
    registry = InstrumentRegistry()
    app = DwfMcpApp(device, registry)
    app.register_instrument(Scope)
    app.register_instrument(Supply)
    app.register_instrument(I2C)
    app.register_instrument(AWG)
    app.register_instrument(Pattern)
    app.register_instrument(DIO)
    app.register_instrument(Logic)
    return app
```

Also update `main()` to read `DWF_ENABLE_VCD`:

```python
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    _vcd_env = os.environ.get("DWF_ENABLE_VCD")
    _enable_vcd: bool | None = None
    if _vcd_env == "1":
        _enable_vcd = True
    elif _vcd_env == "0":
        _enable_vcd = False

    app = build_app(enable_vcd=_enable_vcd)
    ...  # rest of main() unchanged
```

- [ ] **Step 5: Run VCD writer tests**

```
pytest tests/unit/test_vcd_writer.py -v
```
Expected: all 7 tests pass (2 original + 5 new)

- [ ] **Step 6: Run full unit suite**

```
pytest tests/unit/ -v
```
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add src/dwf_mcp/vcd_writer.py src/dwf_mcp/server.py tests/unit/test_vcd_writer.py
git commit -m "feat: add VcdStreamWriter for incremental VCD output; add enable_vcd runtime config"
```

---

## Task 6: Wire VCD streaming into logic.record_start

**Files:**
- Modify: `src/dwf_mcp/instruments/logic.py`
- Modify: `tests/unit/test_logic.py`

### Background

When `format="vcd"` is passed to `logic.record_start`, a `VcdStreamWriter` is opened at start time and stored in `session.meta["vcd_writer"]`. An `on_chunk_sync` closure is created that slices raw chunks to configured pins and writes transitions incrementally. Chunks are NOT accumulated in `session.chunks` for VCD format (write-through avoids double memory).

`record_stop` for VCD format returns the path from `session.meta["vcd_path"]` instead of concatenating chunks; the VCD writer is closed in the `finally` block.

- [ ] **Step 1: Add VCD streaming tests to `tests/unit/test_logic.py`**

Append to the existing file:

```python

# --- VCD streaming record tests ---

@pytest.mark.asyncio
async def test_record_start_vcd_opens_vcd_writer(logic: Logic) -> None:
    vcd = pytest.importorskip("vcd")
    result = await logic.record_start(
        pins=["dio0", "dio1"],
        sample_rate_hz=1_000_000.0,
        duration_s=0.01,
        format="vcd",
    )
    record_id = result["record_id"]
    session = logic._sessions[record_id]
    assert session.meta["vcd_writer"] is not None
    assert session.on_chunk_sync is not None
    # chunks should NOT be accumulated for VCD format
    await asyncio.sleep(0.05)
    assert session.chunks == []
    await logic.record_stop(record_id)


@pytest.mark.asyncio
async def test_record_start_vcd_on_chunk_sync_slices_pins(logic: Logic, tmp_path: Path) -> None:
    vcd = pytest.importorskip("vcd")
    import numpy as np as _np
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # raw chunk has 16 channels; configured pins are dio0 and dio1 (indices 0,1)
    raw = _np.zeros((4, 16), dtype=_np.uint8)
    raw[:, 0] = [0, 1, 1, 0]
    raw[:, 1] = [1, 1, 0, 0]
    fake._logic_record_canned_chunk = raw
    out_path = tmp_path / "test_stream.vcd"
    result = await logic.record_start(
        pins=["dio0", "dio1"],
        sample_rate_hz=1_000_000.0,
        duration_s=0.01,
        format="vcd",
        output_path=str(out_path),
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    stop = await logic.record_stop(record_id)
    assert stop["artifact_path"] == str(out_path)
    assert out_path.exists()
    content = out_path.read_text()
    assert "dio0" in content
    assert "dio1" in content


@pytest.mark.asyncio
async def test_record_stop_vcd_closes_writer(logic: Logic) -> None:
    vcd = pytest.importorskip("vcd")
    result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000.0, duration_s=0.01, format="vcd"
    )
    record_id = result["record_id"]
    session = logic._sessions[record_id]
    vcd_w = session.meta["vcd_writer"]
    await asyncio.sleep(0.05)
    await logic.record_stop(record_id)
    assert vcd_w._closed is True


@pytest.mark.asyncio
async def test_record_start_vcd_disabled_raises(logic: Logic) -> None:
    logic.device.vcd_enabled = False
    with pytest.raises(ValueError, match="VCD output is disabled"):
        await logic.record_start(
            pins=["dio0"], sample_rate_hz=1_000_000.0, duration_s=0.01, format="vcd"
        )
```

- [ ] **Step 2: Run new tests to confirm they fail (VCD wiring not yet in place)**

```
pytest tests/unit/test_logic.py -k "vcd" -v
```
Expected: `test_record_start_vcd_opens_vcd_writer` fails (vcd_writer is None in meta)

- [ ] **Step 3: Update `logic.record_start` to wire in VcdStreamWriter**

In `src/dwf_mcp/instruments/logic.py`, replace the `record_start` method with:

```python
    async def record_start(
        self,
        pins: list[str],
        sample_rate_hz: float,
        duration_s: float,
        output_path: str | None = None,
        format: str = "npz",
        on_chunk: Callable[[str, np.ndarray], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if format not in _VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(_VALID_FORMATS)}, got {format!r}")
        if format == "vcd" and not self.device.vcd_enabled:
            raise ValueError(
                "VCD output is disabled (set DWF_ENABLE_VCD=1 or install dwf-mcp[vcd])"
            )
        self.device.allocator.claim("logic", pins)
        vcd_w: vcd_writer.VcdStreamWriter | None = None
        vcd_path: str | None = None
        try:
            if format == "vcd":
                if output_path:
                    resolved_path = Path(output_path)
                else:
                    resolved_path = (
                        self.artifacts.workspace / "captures"
                        / f"logic_{uuid.uuid4().hex[:8]}.vcd"
                    )
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                vcd_w = vcd_writer.VcdStreamWriter(resolved_path, pins, sample_rate_hz)
                vcd_path = str(resolved_path)
            self.device.backend.logic_record_configure(
                pin_mask=_pins_to_mask(pins),
                sample_rate_hz=sample_rate_hz,
                duration_s=duration_s,
            )
            self.device.backend.logic_record_arm()
        except Exception:
            if vcd_w is not None:
                try:
                    vcd_w.close()
                except Exception:
                    pass
            self.device.allocator.release("logic")
            raise

        record_id = str(uuid.uuid4())
        queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue(maxsize=32)

        on_chunk_sync_fn: Callable[[np.ndarray], None] | None = None
        if vcd_w is not None:
            pin_idx = _pin_indices(pins)
            _vcd_w = vcd_w
            def on_chunk_sync_fn(raw_chunk: np.ndarray) -> None:  # noqa: E306
                sliced = raw_chunk[:, pin_idx].astype(np.uint8)
                _vcd_w.write_chunk(sliced)

        session = RecordingSession(
            record_id=record_id,
            task=None,
            notification_task=None,
            queue=queue,
            chunks=[],
            lost_samples=0,
            done=False,
            error=None,
            on_chunk=on_chunk,
            on_chunk_sync=on_chunk_sync_fn,
            meta={
                "pins": list(pins),
                "sample_rate_hz": sample_rate_hz,
                "output_path": output_path,
                "format": format,
                "vcd_writer": vcd_w,
                "vcd_path": vcd_path,
            },
        )
        try:
            session.task = asyncio.create_task(
                record_loop(
                    session,
                    self.device.backend.logic_record_status,
                    self.device.backend.logic_record_read,
                )
            )
            if on_chunk is not None:
                session.notification_task = asyncio.create_task(
                    notification_loop(session, on_chunk)
                )
        except Exception:
            if session.task is not None:
                session.task.cancel()
            try:
                self.device.backend.logic_record_stop()
            except Exception:
                pass
            if vcd_w is not None:
                try:
                    vcd_w.close()
                except Exception:
                    pass
            self.device.allocator.release("logic")
            raise

        self._sessions[record_id] = session
        return {"record_id": record_id}
```

- [ ] **Step 4: Run VCD tests**

```
pytest tests/unit/test_logic.py -v
```
Expected: all tests pass including new VCD tests

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/logic.py tests/unit/test_logic.py
git commit -m "feat: wire VcdStreamWriter into logic.record_start for bounded-memory VCD streaming"
```

---

## Task 7: MCP push notifications in server.py

**Files:**
- Modify: `src/dwf_mcp/server.py`
- Modify: `tests/unit/test_server_async.py`

### Background

`call_tool` gains an `on_record_chunk` parameter. `_make_instrument_handler` accepts it and injects `on_chunk=on_record_chunk` into kwargs when `method_name == "record_start"`. In `main()`, a closure is created from `server.request_context.session.send_log_message` to deliver base64-encoded chunk data as MCP log messages.

- [ ] **Step 1: Add notification tests to `tests/unit/test_server_async.py`**

Append to the existing file:

```python


@pytest.mark.asyncio
async def test_on_record_chunk_injected_for_logic_record_start(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.open()

    chunks_seen: list[Any] = []
    async def on_chunk(record_id: str, chunk: Any) -> None:
        chunks_seen.append((record_id, chunk))

    result = await app.call_tool(
        "logic.record_start",
        {"pins": ["dio0"], "sample_rate_hz": 100.0, "duration_s": 0.1},
        on_record_chunk=on_chunk,
    )
    assert "record_id" in result
    record_id = result["record_id"]

    await asyncio.sleep(0.05)

    stop = await app.call_tool("logic.record_stop", {"record_id": record_id})
    assert stop.get("error") is None


@pytest.mark.asyncio
async def test_on_record_chunk_not_injected_for_non_record_start(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.open()

    injected: list[Any] = []
    async def on_chunk(record_id: str, chunk: Any) -> None:
        injected.append(chunk)

    result = await app.call_tool(
        "logic.configure",
        {"pins": ["dio0"], "sample_rate_hz": 1_000_000.0, "buffer_size": 1024},
        on_record_chunk=on_chunk,
    )
    assert result.get("configured") is True
    assert injected == []


@pytest.mark.asyncio
async def test_call_tool_without_on_record_chunk_still_works(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.open()

    result = await app.call_tool(
        "logic.record_start",
        {"pins": ["dio0"], "sample_rate_hz": 100.0, "duration_s": 0.1},
    )
    assert "record_id" in result
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    stop = await app.call_tool("logic.record_stop", {"record_id": record_id})
    assert stop.get("error") is None


def test_build_app_registers_stage3b_streaming_tools(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    tool_names = set(app._tools)
    expected = {
        "scope.record_start", "scope.record_status", "scope.record_stop",
    }
    missing = expected - tool_names
    assert missing == set(), f"missing tools: {missing}"
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/unit/test_server_async.py -v
```
Expected: new tests fail — `call_tool` doesn't accept `on_record_chunk` yet

- [ ] **Step 3: Update `src/dwf_mcp/server.py`**

Update `DwfMcpApp.call_tool` and `_make_instrument_handler`:

```python
    def _make_instrument_handler(self, instrument_name: str, method_name: str) -> Any:
        async def handler(
            on_record_chunk: Any = None,
            **kwargs: Any,
        ) -> Any:
            instrument = self._get_or_create_instrument(instrument_name)
            method = getattr(instrument, method_name)
            if method_name == "record_start" and on_record_chunk is not None:
                kwargs["on_chunk"] = on_record_chunk
            result = method(**kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result
        return handler

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        on_record_chunk: Any = None,
    ) -> dict[str, Any]:
        try:
            handler = self._tools[name]
        except KeyError:
            raise ValueError(f"unknown tool {name!r}") from None
        self.device.tick_idle()
        try:
            result = await handler(on_record_chunk=on_record_chunk, **args)
            return cast(dict[str, Any], result)
        except tuple(_ERROR_TYPES.keys()) as exc:
            return {
                "error": {
                    "type": _ERROR_TYPES[type(exc)],
                    "message": str(exc),
                    "details": getattr(exc, "details", {}),
                }
            }
```

Also update `main()` to create the `on_chunk` closure:

```python
def main() -> None:
    """Stdio MCP transport entry point. Wires DwfMcpApp into the mcp SDK."""
    import base64
    import json as _json
    logging.basicConfig(level=logging.INFO)
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    _vcd_env = os.environ.get("DWF_ENABLE_VCD")
    _enable_vcd: bool | None = None
    if _vcd_env == "1":
        _enable_vcd = True
    elif _vcd_env == "0":
        _enable_vcd = False

    app = build_app(enable_vcd=_enable_vcd)
    server: Server = Server("dwf-mcp")

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[dict[str, Any]]:
        return [
            {"name": name, "description": "", "inputSchema": app._tool_schemas[name]}  # noqa: SLF001
            for name in app._tools  # noqa: SLF001
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        mcp_session = server.request_context.session

        async def on_chunk(record_id: str, chunk: Any) -> None:
            import numpy as np
            arr = np.asarray(chunk)
            await mcp_session.send_log_message(
                level="info",
                data=_json.dumps({
                    "event": "record_chunk",
                    "record_id": record_id,
                    "n_samples": int(arr.shape[0]),
                    "dtype": str(arr.dtype),
                    "shape": list(arr.shape),
                    "data_b64": base64.b64encode(arr.tobytes()).decode(),
                }),
            )

        return await app.call_tool(name, arguments, on_record_chunk=on_chunk)

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())
```

- [ ] **Step 4: Run server tests**

```
pytest tests/unit/test_server_async.py -v
```
Expected: all tests pass

- [ ] **Step 5: Run full unit suite**

```
pytest tests/unit/ -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add src/dwf_mcp/server.py tests/unit/test_server_async.py
git commit -m "feat: MCP push notifications for record_start — on_record_chunk injected via call_tool"
```

---

## Task 8: PydwfBackend scope_record_*

**Files:**
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`

### Background

Implements `scope_record_configure/arm/status/read/stop` using pydwf's `analogIn` record-mode API. The instrument layer slices the returned (N, 2) float64 array to configured channels in `record_stop`. Both channels are always read to keep the record buffer drained — unused channel data is discarded in the instrument.

- [ ] **Step 1: Add scope record methods to `pydwf_backend.py`**

Find the `# Scope (AnalogIn)` section in `pydwf_backend.py` and append after the existing scope methods:

```python
    # Scope record-mode (AnalogIn streaming) — added in stage 3b.

    def scope_record_configure(
        self,
        channels: list[int],
        range_v: float,
        offset_v: float,
        coupling: str,
        sample_rate_hz: float,
        duration_s: float,
    ) -> None:
        from pydwf.core.auxiliary.enum_types import DwfAcquisitionMode, DwfAnalogInCoupling
        coupling_map = {
            "DC": DwfAnalogInCoupling.DC,
            "AC": DwfAnalogInCoupling.AC,
        }
        ai = self._device.analogIn
        for ch in (0, 1):  # 0-indexed; channels list uses 1-indexed
            ai.channelEnableSet(ch, (ch + 1) in channels)
            ai.channelRangeSet(ch, range_v)
            ai.channelOffsetSet(ch, offset_v)
            ai.channelCouplingSet(ch, coupling_map[coupling])
        ai.frequencySet(sample_rate_hz)
        ai.acquisitionModeSet(DwfAcquisitionMode.Record)
        ai.recordLengthSet(duration_s)

    def scope_record_arm(self) -> None:
        self._device.analogIn.configure(False, True)

    def scope_record_status(self) -> tuple[int, int, int]:
        """Poll acquisition state. Returns (available, lost, remaining) in samples."""
        self._device.analogIn.status(True)
        available, lost, remaining = self._device.analogIn.statusRecord()
        return int(available), int(lost), int(remaining)

    def scope_record_read(self, count: int) -> np.ndarray:
        """Read `count` samples from both analog channels. Returns shape (count, 2) float64."""
        ai = self._device.analogIn
        ch1 = ai.statusData(0, count)
        ch2 = ai.statusData(1, count)
        return np.column_stack([ch1, ch2]).astype(np.float64)

    def scope_record_stop(self) -> None:
        self._device.analogIn.reset()
```

- [ ] **Step 2: Run full unit suite (no hardware required)**

```
pytest tests/unit/ -v
```
Expected: all tests pass (pydwf_backend code is not exercised in unit tests)

- [ ] **Step 3: Commit**

```bash
git add src/dwf_mcp/backends/pydwf_backend.py
git commit -m "feat: implement scope_record_* in PydwfBackend using AnalogIn record mode"
```

---

## Task 9: Hardware smoke tests

**Files:**
- Create: `tests/hardware/test_scope_record_hardware.py`

### Background

Scope record hardware tests require the AD3 probing a known signal. The minimum viable smoke test is a DC signal (e.g., W1 AWG set to DC offset) probed by scope ch1. Record for 0.5s and verify the artifact contains the expected DC level.

All hardware tests are deselected by default with `pytest -m "not hardware"`.

- [ ] **Step 1: Create `tests/hardware/test_scope_record_hardware.py`**

```python
"""Hardware smoke tests for scope.record_start/stop.

Wiring required:
    W1 (AWG ch1 output) → scope ch1 (1+ / 1-)
    W2 (AWG ch2 output, optional) → scope ch2

Run with:
    pytest tests/hardware/test_scope_record_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("scope_record")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    app.call_tool_sync = lambda name, args: asyncio.get_event_loop().run_until_complete(
        app.call_tool(name, args)
    )
    result = asyncio.get_event_loop().run_until_complete(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.get_event_loop().run_until_complete(app.call_tool("waveforms.close", {}))


@pytest.mark.asyncio
async def test_scope_record_dc_signal(app, tmp_path: Path) -> None:
    """Record a DC signal from W1 and verify mean voltage is approximately correct."""
    # Set W1 to DC at 2.0V
    await app.call_tool("awg.configure", {
        "channel": 1,
        "function": "DC",
        "frequency_hz": 1000.0,
        "amplitude_v": 0.0,
        "offset_v": 2.0,
        "phase_deg": 0.0,
    })
    await app.call_tool("awg.start", {"channel": 1})

    out_path = tmp_path / "scope_record_dc.npz"
    result = await app.call_tool("scope.record_start", {
        "channels": [1],
        "range_v": 5.0,
        "sample_rate_hz": 100_000.0,
        "duration_s": 0.2,
        "output_path": str(out_path),
    })
    record_id = result["record_id"]

    # Wait for completion
    for _ in range(50):
        status = await app.call_tool("scope.record_status", {"record_id": record_id})
        if status["done"]:
            break
        await asyncio.sleep(0.05)

    stop = await app.call_tool("scope.record_stop", {"record_id": record_id})
    await app.call_tool("awg.stop", {"channel": 1})

    assert stop["artifact_error"] is None, f"artifact_error: {stop['artifact_error']}"
    assert stop["artifact_path"] is not None
    assert Path(stop["artifact_path"]).exists()
    assert stop["lost_samples"] == 0, f"lost {stop['lost_samples']} samples"

    data = np.load(stop["artifact_path"])
    assert "ch1" in data
    mean_v = float(data["ch1"].mean())
    assert abs(mean_v - 2.0) < 0.3, f"expected ~2.0V DC, got {mean_v:.3f}V"


@pytest.mark.asyncio
async def test_scope_record_two_channels(app, tmp_path: Path) -> None:
    """Record both channels simultaneously."""
    # W1 = 1.5V DC, W2 = -1.0V DC (if wired)
    await app.call_tool("awg.configure", {
        "channel": 1, "function": "DC", "frequency_hz": 1000.0,
        "amplitude_v": 0.0, "offset_v": 1.5, "phase_deg": 0.0,
    })
    await app.call_tool("awg.start", {"channel": 1})
    await app.call_tool("awg.configure", {
        "channel": 2, "function": "DC", "frequency_hz": 1000.0,
        "amplitude_v": 0.0, "offset_v": -1.0, "phase_deg": 0.0,
    })
    await app.call_tool("awg.start", {"channel": 2})

    result = await app.call_tool("scope.record_start", {
        "channels": [1, 2],
        "range_v": 5.0,
        "sample_rate_hz": 50_000.0,
        "duration_s": 0.1,
    })
    record_id = result["record_id"]

    for _ in range(30):
        status = await app.call_tool("scope.record_status", {"record_id": record_id})
        if status["done"]:
            break
        await asyncio.sleep(0.05)

    stop = await app.call_tool("scope.record_stop", {"record_id": record_id})
    await app.call_tool("awg.stop", {"channel": 1})
    await app.call_tool("awg.stop", {"channel": 2})

    assert stop["artifact_error"] is None
    assert stop["artifact_path"] is not None
    data = np.load(stop["artifact_path"])
    assert "ch1" in data and "ch2" in data
    assert abs(float(data["ch1"].mean()) - 1.5) < 0.3
    assert abs(float(data["ch2"].mean()) - (-1.0)) < 0.3


@pytest.mark.asyncio
async def test_scope_record_buffer_to_record_transition(app) -> None:
    """Starting record_start while in buffer mode cleanly transitions modes."""
    # First enter buffer mode
    await app.call_tool("scope.configure", {
        "channels": [1], "range_v": 5.0,
        "sample_rate_hz": 10_000.0, "buffer_size": 1024,
    })
    # Now switch to record mode (should implicitly release buffer)
    result = await app.call_tool("scope.record_start", {
        "channels": [1], "range_v": 5.0,
        "sample_rate_hz": 10_000.0, "duration_s": 0.05,
    })
    assert "record_id" in result
    record_id = result["record_id"]
    for _ in range(20):
        status = await app.call_tool("scope.record_status", {"record_id": record_id})
        if status["done"]:
            break
        await asyncio.sleep(0.05)
    stop = await app.call_tool("scope.record_stop", {"record_id": record_id})
    assert stop["error"] is None
```

- [ ] **Step 2: Run hardware tests (requires connected AD3)**

```
pytest tests/hardware/test_scope_record_hardware.py -v -m hardware
```
Expected: 3 passed (with W1 → scope ch1 and W2 → scope ch2 wired)

- [ ] **Step 3: Commit**

```bash
git add tests/hardware/test_scope_record_hardware.py
git commit -m "test: add scope.record hardware smoke tests"
```

---

## Final: verify full test suite

- [ ] **Run full unit + integration suite**

```
pytest tests/unit/ tests/integration/ -v
```
Expected: ~220+ passed (148 pre-3b + ~70 new), 0 failed

- [ ] **Verify tool count**

```python
from dwf_mcp.server import build_app
app = build_app(backend_name="fake", workspace="/tmp")
print(f"Total tools: {len(app._tools)}")
# Expected: 43 (29 pre-3a tools + 14 new = 43, including 3 scope.record)
# Note: Plan A adds DMM/SPI/UART/CAN bringing total to 43 when both plans are complete.
# This plan alone (streaming/VCD) adds 3 scope.record tools: 29 → 32 tools.
```

- [ ] **Final commit**

```bash
git add docs/superpowers/plans/2026-06-03-stage3b-streaming-vcd.md
git commit -m "docs: add stage3b streaming+VCD implementation plan"
```
