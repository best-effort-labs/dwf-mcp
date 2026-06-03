# Stage 3a Implementation Plan: AWG, Logic, Pattern, DIO

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AWG, Logic (buffer-mode + streaming + VCD), Pattern, and DIO instruments to dwf-mcp, bringing the server from 13 to 29 tools.

**Architecture:** Four `Instrument` subclasses following stage 2 patterns (partial-failure rollback, pin allocator, safety gate). Logic streaming uses `asyncio.Task` + `_RecordingSession` dataclass. VCD output is a pyvcd optional extra. Server handler updated to detect and await coroutines.

**Tech Stack:** Python 3.12, pydwf 1.1.x, asyncio, numpy, pyvcd (optional extra)

**Spec:** `docs/superpowers/specs/2026-06-03-stage3a-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/dwf_mcp/backend.py` | Modify | Add `NotImplementedError` stubs for all 17 new backend methods |
| `src/dwf_mcp/backends/fake.py` | Modify | Add recording stubs + canned-response state for all new methods |
| `src/dwf_mcp/backends/pydwf_backend.py` | Modify | Implement all new backend methods against pydwf |
| `src/dwf_mcp/device.py` | Modify | Add `pattern_start` kind to `_check_policy` |
| `src/dwf_mcp/policy.py` | Modify | Add `check_pattern_voltage` method |
| `src/dwf_mcp/server.py` | Modify | Make `_make_instrument_handler` await coroutines; register 4 new instruments |
| `src/dwf_mcp/vcd_writer.py` | Create | Thin pyvcd wrapper: `write(path, samples, pin_names, sample_rate_hz)` |
| `src/dwf_mcp/instruments/awg.py` | Create | AWG instrument (configure/upload_custom/start/stop) |
| `src/dwf_mcp/instruments/pattern.py` | Create | Pattern instrument (configure/start/stop per-pin) |
| `src/dwf_mcp/instruments/dio.py` | Create | DIO instrument (set_direction/set/read transient-claim) |
| `src/dwf_mcp/instruments/logic.py` | Create | Logic instrument: buffer-mode + streaming + VCD |
| `pyproject.toml` | Modify | Add `vcd = ["pyvcd"]` optional extra; add pyvcd to dev extra |
| `tests/unit/test_awg.py` | Create | AWG unit tests |
| `tests/unit/test_pattern.py` | Create | Pattern unit tests |
| `tests/unit/test_dio.py` | Create | DIO unit tests |
| `tests/unit/test_logic.py` | Create | Logic unit tests (buffer + streaming + VCD) |
| `tests/unit/test_vcd_writer.py` | Create | VCD writer round-trip test |
| `tests/hardware/test_awg_hardware.py` | Create | AWG smoke test (W1→scope ch1+) |
| `tests/hardware/test_logic_hardware.py` | Create | Logic + Pattern smoke test (DIO0↔DIO1 loopback) |
| `tests/hardware/test_dio_hardware.py` | Create | DIO smoke test (DIO0 out, DIO1 in) |

---

## Task 1: Backend stubs + FakeBackend + server async fix

**Files:**
- Modify: `src/dwf_mcp/backend.py`
- Modify: `src/dwf_mcp/backends/fake.py`
- Modify: `src/dwf_mcp/server.py`

### Background

`backend.py` uses `raise NotImplementedError` (not `@abstractmethod`) for instrument-layer methods, so new instruments can be added without breaking existing backends. `fake.py` records calls as `list[tuple[str, dict]]` per instrument group. `server.py`'s `_make_instrument_handler` currently does `return method(**kwargs)` with no `await` — this breaks `record_start` and `record_stop` which must be `async def`.

- [ ] **Step 1: Write the failing server async test**

Create `tests/unit/test_server_async.py`:

```python
"""Verify that _make_instrument_handler awaits coroutine methods."""
from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from dwf_mcp.instrument import Instrument
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice


class _AsyncInstrument(Instrument):
    name = "async_test"
    tools: ClassVar[dict[str, Any]] = {
        "do_async": ("do_async", {"type": "object", "properties": {}}),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        pass

    async def do_async(self) -> dict[str, Any]:
        return {"async": True}

    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_handler_awaits_coroutine(tmp_path):
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.registry.register(_AsyncInstrument)
    app._tools["async_test.do_async"] = app._make_instrument_handler("async_test", "do_async")
    app.instruments["async_test"] = _AsyncInstrument(device=app.device, artifacts=app.artifacts)
    result = await app.call_tool("async_test.do_async", {})
    assert result == {"async": True}
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp
.venv/bin/pytest tests/unit/test_server_async.py -v
```

Expected: FAIL — the coroutine is returned unawaited, result is a coroutine object not a dict.

- [ ] **Step 3: Fix `_make_instrument_handler` in server.py**

Edit `src/dwf_mcp/server.py`, replace `_make_instrument_handler`:

```python
def _make_instrument_handler(self, instrument_name: str, method_name: str) -> Any:
    async def handler(**kwargs: Any) -> Any:
        instrument = self._get_or_create_instrument(instrument_name)
        method = getattr(instrument, method_name)
        result = method(**kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result
    return handler
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
.venv/bin/pytest tests/unit/test_server_async.py -v
```

Expected: PASS

- [ ] **Step 5: Add backend stubs to `backend.py`**

Append after the existing I2C block in `src/dwf_mcp/backend.py`:

```python
    # AWG (AnalogOut) — added in stage 3a.
    def awg_configure(
        self, channel: int, function: str, freq_hz: float,
        amplitude_v: float, offset_v: float, phase_deg: float,
        symmetry: float, run_time_s: float | None,
    ) -> None:
        raise NotImplementedError

    def awg_upload_custom(self, channel: int, samples: np.ndarray) -> None:
        raise NotImplementedError

    def awg_start(self, channel: int) -> None:
        raise NotImplementedError

    def awg_stop(self, channel: int) -> None:
        raise NotImplementedError

    # Pattern (DigitalOut) — added in stage 3a.
    def pattern_configure(
        self, pin_idx: int, function: str, freq_hz: float,
        duty: float, idle_state: str,
    ) -> None:
        raise NotImplementedError

    def pattern_start(self, pin_idx: int) -> None:
        raise NotImplementedError

    def pattern_stop(self, pin_idx: int) -> None:
        raise NotImplementedError

    # DIO (DigitalIO) — added in stage 3a.
    def dio_set_direction(self, pin_idx: int, output: bool) -> None:
        raise NotImplementedError

    def dio_set(self, pin_idx: int, state: bool) -> None:
        raise NotImplementedError

    def dio_read(self, pin_idx: int) -> bool:
        raise NotImplementedError

    # Logic buffer-mode (DigitalIn) — added in stage 3a.
    def logic_configure(
        self, pin_mask: int, sample_rate_hz: float, buffer_size: int
    ) -> None:
        raise NotImplementedError

    def logic_set_trigger(
        self, source: str, pin_idx: int | None, level: float | None,
        condition: str | None, position_s: float | None, timeout_s: float | None,
    ) -> None:
        raise NotImplementedError

    def logic_arm(self) -> None:
        raise NotImplementedError

    def logic_status(self) -> str:
        raise NotImplementedError

    def logic_read(self, count: int) -> np.ndarray:
        raise NotImplementedError

    # Logic record-mode (DigitalIn streaming) — added in stage 3a.
    def logic_record_configure(self, pin_mask: int, sample_rate_hz: float) -> None:
        raise NotImplementedError

    def logic_record_arm(self) -> None:
        raise NotImplementedError

    def logic_record_status(self) -> tuple[int, int, int]:
        raise NotImplementedError

    def logic_record_read(self, count: int) -> np.ndarray:
        raise NotImplementedError

    def logic_record_stop(self) -> None:
        raise NotImplementedError
```

- [ ] **Step 6: Add FakeBackend state and methods for all new instruments**

Append state initialization in `FakeBackend.__init__` (after the `# I2C` block):

```python
        # AWG (AnalogOut) state
        self.awg_calls: list[tuple[str, dict[str, Any]]] = []
        # Pattern (DigitalOut) state
        self.pattern_calls: list[tuple[str, dict[str, Any]]] = []
        # DIO (DigitalIO) state
        self.dio_calls: list[tuple[str, dict[str, Any]]] = []
        self._dio_pin_values: dict[int, bool] = {}
        # Logic buffer-mode state
        self.logic_calls: list[tuple[str, dict[str, Any]]] = []
        self._logic_status_sequence: list[str] = ["Done"]
        self._logic_status_idx = 0
        self._logic_canned_data: np.ndarray = np.zeros((0, 16), dtype=np.uint8)
        # Logic record-mode state
        self._logic_record_status_sequence: list[tuple[int, int, int]] = [(10, 0, 0)]
        self._logic_record_status_idx = 0
        self._logic_record_canned_chunk: np.ndarray = np.zeros((10, 16), dtype=np.uint8)
```

Append the method implementations to `FakeBackend` (after the I2C methods):

```python
    # --- AWG (AnalogOut) ---

    def awg_configure(
        self, channel: int, function: str, freq_hz: float,
        amplitude_v: float, offset_v: float, phase_deg: float,
        symmetry: float, run_time_s: float | None,
    ) -> None:
        self.awg_calls.append(("configure", {
            "channel": channel, "function": function, "freq_hz": freq_hz,
            "amplitude_v": amplitude_v, "offset_v": offset_v,
            "phase_deg": phase_deg, "symmetry": symmetry, "run_time_s": run_time_s,
        }))

    def awg_upload_custom(self, channel: int, samples: np.ndarray) -> None:
        self.awg_calls.append(("upload_custom", {"channel": channel, "n_samples": len(samples)}))

    def awg_start(self, channel: int) -> None:
        self.awg_calls.append(("start", {"channel": channel}))

    def awg_stop(self, channel: int) -> None:
        self.awg_calls.append(("stop", {"channel": channel}))

    # --- Pattern (DigitalOut) ---

    def pattern_configure(
        self, pin_idx: int, function: str, freq_hz: float,
        duty: float, idle_state: str,
    ) -> None:
        self.pattern_calls.append(("configure", {
            "pin_idx": pin_idx, "function": function, "freq_hz": freq_hz,
            "duty": duty, "idle_state": idle_state,
        }))

    def pattern_start(self, pin_idx: int) -> None:
        self.pattern_calls.append(("start", {"pin_idx": pin_idx}))

    def pattern_stop(self, pin_idx: int) -> None:
        self.pattern_calls.append(("stop", {"pin_idx": pin_idx}))

    # --- DIO (DigitalIO) ---

    def dio_set_direction(self, pin_idx: int, output: bool) -> None:
        self.dio_calls.append(("set_direction", {"pin_idx": pin_idx, "output": output}))

    def dio_set(self, pin_idx: int, state: bool) -> None:
        self._dio_pin_values[pin_idx] = state
        self.dio_calls.append(("set", {"pin_idx": pin_idx, "state": state}))

    def dio_read(self, pin_idx: int) -> bool:
        return self._dio_pin_values.get(pin_idx, False)

    # --- Logic buffer-mode (DigitalIn) ---

    def logic_configure(
        self, pin_mask: int, sample_rate_hz: float, buffer_size: int
    ) -> None:
        self.logic_calls.append(("configure", {
            "pin_mask": pin_mask, "sample_rate_hz": sample_rate_hz, "buffer_size": buffer_size,
        }))
        self._logic_status_idx = 0

    def logic_set_trigger(
        self, source: str, pin_idx: int | None, level: float | None,
        condition: str | None, position_s: float | None, timeout_s: float | None,
    ) -> None:
        self.logic_calls.append(("set_trigger", {
            "source": source, "pin_idx": pin_idx, "level": level,
            "condition": condition, "position_s": position_s, "timeout_s": timeout_s,
        }))

    def logic_arm(self) -> None:
        self.logic_calls.append(("arm", {}))

    def logic_status(self) -> str:
        idx = min(self._logic_status_idx, len(self._logic_status_sequence) - 1)
        result = self._logic_status_sequence[idx]
        self._logic_status_idx += 1
        return result

    def logic_read(self, count: int) -> np.ndarray:
        if len(self._logic_canned_data) >= count:
            return self._logic_canned_data[:count]
        return np.zeros((count, 16), dtype=np.uint8)

    # --- Logic record-mode ---

    def logic_record_configure(self, pin_mask: int, sample_rate_hz: float) -> None:
        self.logic_calls.append(("record_configure", {
            "pin_mask": pin_mask, "sample_rate_hz": sample_rate_hz,
        }))
        self._logic_record_status_idx = 0

    def logic_record_arm(self) -> None:
        self.logic_calls.append(("record_arm", {}))

    def logic_record_status(self) -> tuple[int, int, int]:
        idx = min(self._logic_record_status_idx, len(self._logic_record_status_sequence) - 1)
        result = self._logic_record_status_sequence[idx]
        self._logic_record_status_idx += 1
        return result

    def logic_record_read(self, count: int) -> np.ndarray:
        return self._logic_record_canned_chunk[:count]

    def logic_record_stop(self) -> None:
        self.logic_calls.append(("record_stop", {}))

    # Test helpers for logic
    def set_logic_status_sequence(self, sequence: list[str]) -> None:
        self._logic_status_sequence = list(sequence)
        self._logic_status_idx = 0

    def set_logic_record_status_sequence(
        self, sequence: list[tuple[int, int, int]]
    ) -> None:
        self._logic_record_status_sequence = list(sequence)
        self._logic_record_status_idx = 0
```

- [ ] **Step 7: Run full test suite to confirm baseline still passes**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```

Expected: all existing tests pass + new server_async test passes. Count ≥ 108.

- [ ] **Step 8: Commit**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/fake.py src/dwf_mcp/server.py tests/unit/test_server_async.py
git commit -m "feat: backend stubs, FakeBackend stubs, server async handler fix for stage 3a"
```

---

## Task 2: VCD writer + pyproject.toml optional extra

**Files:**
- Create: `src/dwf_mcp/vcd_writer.py`
- Modify: `pyproject.toml`
- Create: `tests/unit/test_vcd_writer.py`

### Background

pyvcd PyPI package (`pip install pyvcd`), imports as `import vcd`. `VCDWriter` takes a file object, `timescale` string, and `date` string. Variables are registered with `register_var(scope, name, var_type, size)` then values written with `change(var, time, value)`. The import name is `vcd`, not `pyvcd`.

- [ ] **Step 1: Add pyvcd optional extra to pyproject.toml**

Edit `pyproject.toml`, replace the `[project.optional-dependencies]` section:

```toml
[project.optional-dependencies]
vcd = ["pyvcd>=2.0"]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
    "ruff>=0.4",
    "mypy>=1.10",
    "pyvcd>=2.0",
]
```

- [ ] **Step 2: Install updated dev extras**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp
.venv/bin/pip install --quiet -e ".[dev]"
```

Expected: pyvcd installed.

- [ ] **Step 3: Write the failing VCD writer test**

Create `tests/unit/test_vcd_writer.py`:

```python
"""Round-trip test for vcd_writer: write a synthetic uint8 array, read it back."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

vcd = pytest.importorskip("vcd")


def test_vcd_writer_round_trips_transitions(tmp_path: Path) -> None:
    from dwf_mcp.vcd_writer import write as vcd_write

    # 4 samples, 2 pins. Pin 0 toggles at sample 2; pin 1 stays low.
    samples = np.array(
        [[0, 0], [0, 0], [1, 0], [1, 0]],
        dtype=np.uint8,
    )
    pin_names = ["dio0", "dio1"]
    out_path = tmp_path / "test.vcd"
    vcd_write(out_path, samples, pin_names, sample_rate_hz=1_000_000.0)

    assert out_path.exists()
    content = out_path.read_text()
    # VCD file must contain variable declarations and a time step
    assert "$var" in content
    assert "dio0" in content
    assert "dio1" in content
    # The toggle at sample 2 → timescale 1us → time 2us
    assert "#2" in content


def test_vcd_writer_missing_package_raises(tmp_path: Path, monkeypatch) -> None:
    import dwf_mcp.vcd_writer as vw
    monkeypatch.setattr(vw, "HAS_VCD", False)

    samples = np.zeros((4, 2), dtype=np.uint8)
    with pytest.raises(ImportError, match="pyvcd"):
        vw.write(tmp_path / "out.vcd", samples, ["a", "b"], 1_000_000.0)
```

- [ ] **Step 4: Run test to confirm it fails**

```bash
.venv/bin/pytest tests/unit/test_vcd_writer.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'dwf_mcp.vcd_writer'`

- [ ] **Step 5: Create `src/dwf_mcp/vcd_writer.py`**

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


def write(
    path: Path,
    samples: np.ndarray,
    pin_names: list[str],
    sample_rate_hz: float,
) -> None:
    """Write samples (uint8, shape (n_samples, n_pins)) to a VCD file.

    Raises ImportError if pyvcd is not installed.
    """
    if not HAS_VCD:
        raise ImportError(
            "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
        )

    n_samples, n_pins = samples.shape
    # Compute timescale: pick ns or us depending on sample rate.
    # 1 sample = 1/sample_rate_hz seconds.
    period_s = 1.0 / sample_rate_hz
    if period_s < 1e-9:
        timescale = "1 ps"
        time_scale_factor = int(round(period_s * 1e12))
    elif period_s < 1e-6:
        timescale = "1 ns"
        time_scale_factor = int(round(period_s * 1e9))
    elif period_s < 1e-3:
        timescale = "1 us"
        time_scale_factor = int(round(period_s * 1e6))
    else:
        timescale = "1 ms"
        time_scale_factor = int(round(period_s * 1e3))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as f:
        with _vcd.VCDWriter(f, timescale=timescale, date="today") as writer:
            # Register one variable per pin.
            vars_: list[Any] = [
                writer.register_var("logic", name, "wire", size=1)
                for name in pin_names
            ]
            # Emit initial values at time 0.
            for i, var in enumerate(vars_):
                writer.change(var, 0, int(samples[0, i]))

            # Iterate samples, emit only on transitions.
            prev = samples[0].copy()
            for sample_idx in range(1, n_samples):
                t = sample_idx * time_scale_factor
                row = samples[sample_idx]
                for pin_idx in range(n_pins):
                    if row[pin_idx] != prev[pin_idx]:
                        writer.change(vars_[pin_idx], t, int(row[pin_idx]))
                prev = row.copy()
```

- [ ] **Step 6: Run test to confirm it passes**

```bash
.venv/bin/pytest tests/unit/test_vcd_writer.py -v
```

Expected: PASS (both test cases)

- [ ] **Step 7: Commit**

```bash
git add src/dwf_mcp/vcd_writer.py tests/unit/test_vcd_writer.py pyproject.toml
git commit -m "feat: vcd_writer module (pyvcd optional extra) + test"
```

---

## Task 3: AWG instrument + pydwf backend

**Files:**
- Create: `src/dwf_mcp/instruments/awg.py`
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/unit/test_awg.py`

### Background

AWG uses `device.analogOut`. pydwf `AnalogOut` uses `DwfAnalogOutNode.Carrier` as the node for all parameters on the AD3 (the carrier node is the actual signal generator; modulation nodes are unused in our API). Key calls:
- `analogOut.nodeEnableSet(ch_idx, DwfAnalogOutNode.Carrier, True)`
- `analogOut.nodeFunctionSet(ch_idx, DwfAnalogOutNode.Carrier, DwfAnalogOutFunction.Sine)`
- `analogOut.nodeFrequencySet(ch_idx, DwfAnalogOutNode.Carrier, freq_hz)`
- `analogOut.nodeAmplitudeSet(ch_idx, DwfAnalogOutNode.Carrier, amplitude_v)`
- `analogOut.nodeOffsetSet(ch_idx, DwfAnalogOutNode.Carrier, offset_v)`  
- `analogOut.nodePhaseSet(ch_idx, DwfAnalogOutNode.Carrier, phase_deg)`
- `analogOut.nodeSymmetrySet(ch_idx, DwfAnalogOutNode.Carrier, symmetry)` (0-100 percent)
- `analogOut.runSet(ch_idx, run_time_s)` — 0.0 = run indefinitely
- `analogOut.nodeDataSet(ch_idx, DwfAnalogOutNode.Carrier, samples)` — for custom waveform
- `analogOut.configure(ch_idx, start=False)` — apply params without starting
- `analogOut.configure(ch_idx, start=True)` — apply and start

Channel mapping: tool channel 1 → pydwf index 0; channel 2 → index 1.

AWG uses an **accumulating pin claim model** (same as Supply): `awg.configure(channel=1)` claims `["awg1"]`; subsequent `awg.configure(channel=2)` claims `["awg1", "awg2"]`. The `awg_clock` exclusive resource group enforces no other instrument holds either AWG pin.

- [ ] **Step 1: Write the failing AWG unit tests**

Create `tests/unit/test_awg.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.awg import AWG
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(awg_max_amplitude=3.3),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def awg(device: DwfDevice, tmp_path: Path) -> AWG:
    device.open()
    return AWG(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pin(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    assert awg.device.allocator.claimed_pins() == {"awg1": "awg"}


def test_configure_two_channels_accumulates_claims(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.configure(channel=2, function="Square", frequency_hz=500.0, amplitude_v=0.5)
    pins = awg.device.allocator.claimed_pins()
    assert pins == {"awg1": "awg", "awg2": "awg"}


def test_configure_does_not_start_output(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.awg_calls if c[0] == "start"]
    assert starts == []


def test_start_calls_backend_start(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.start(channel=1)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.awg_calls if c[0] == "start"]
    assert len(starts) == 1
    assert starts[0][1] == {"channel": 1}


def test_start_safety_gate_rejects_over_cap(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=5.0)
    with pytest.raises(SafetyViolation):
        awg.start(channel=1)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.awg_calls if c[0] == "start"]
    assert starts == []  # backend never called


def test_stop_calls_backend_stop(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.stop(channel=1)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    stops = [c for c in fake.awg_calls if c[0] == "stop"]
    assert len(stops) == 1


def test_partial_failure_rollback(awg: AWG, monkeypatch: pytest.MonkeyPatch) -> None:
    # Configure ch1 successfully first.
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    prior_pins = dict(awg.device.allocator.claimed_pins())

    # Make the next configure fail at backend.
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("backend exploded")
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    monkeypatch.setattr(fake, "awg_configure", boom)

    with pytest.raises(RuntimeError):
        awg.configure(channel=2, function="Square", frequency_hz=500.0, amplitude_v=0.5)

    # Claim should be restored to prior state (ch1 only).
    assert awg.device.allocator.claimed_pins() == prior_pins


def test_upload_custom_validates_shape(awg: AWG) -> None:
    bad_samples = np.zeros((10, 2), dtype=np.float64)  # 2D, not 1D
    with pytest.raises(ValueError, match="1-D"):
        awg.upload_custom(channel=1, samples_npy_path=None, _samples=bad_samples)


def test_upload_custom_claims_pin(awg: AWG, tmp_path: Path) -> None:
    samples = np.linspace(-1.0, 1.0, 100)
    npy_path = tmp_path / "wave.npy"
    np.save(npy_path, samples)
    awg.upload_custom(channel=1, samples_npy_path=str(npy_path))
    assert "awg1" in awg.device.allocator.claimed_pins()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_awg.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'dwf_mcp.instruments.awg'`

- [ ] **Step 3: Create `src/dwf_mcp/instruments/awg.py`**

```python
"""AWG (AnalogOut) instrument. Two channels (W1/W2), accumulating pin claim model."""
from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_Set = set  # alias — 'set' is a method name on this class

_VALID_FUNCTIONS = frozenset(
    {"Sine", "Square", "Triangle", "RampUp", "RampDown", "DC", "Noise", "Custom"}
)
_CHANNEL_TO_PIN = {1: "awg1", 2: "awg2"}

AWG_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "function", "frequency_hz", "amplitude_v"],
    "properties": {
        "channel": {"type": "integer", "enum": [1, 2]},
        "function": {
            "type": "string",
            "enum": sorted(_VALID_FUNCTIONS),
        },
        "frequency_hz": {"type": "number", "minimum": 0.0},
        "amplitude_v": {"type": "number", "minimum": 0.0},
        "offset_v": {"type": "number", "default": 0.0},
        "phase_deg": {"type": "number", "default": 0.0},
        "symmetry": {"type": "number", "minimum": 0.0, "maximum": 100.0, "default": 50.0},
        "run_time_s": {"type": "number", "minimum": 0.0},
    },
}

AWG_UPLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "samples_npy_path"],
    "properties": {
        "channel": {"type": "integer", "enum": [1, 2]},
        "samples_npy_path": {"type": "string"},
    },
}

AWG_CHANNEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel"],
    "properties": {"channel": {"type": "integer", "enum": [1, 2]}},
}


class AWG(Instrument):
    name = "awg"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":     ("configure",     AWG_CONFIGURE_SCHEMA),
        "upload_custom": ("upload_custom", AWG_UPLOAD_SCHEMA),
        "start":         ("start",         AWG_CHANNEL_SCHEMA),
        "stop":          ("stop",          AWG_CHANNEL_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._amplitude: dict[int, float] = {}
        self._configured_channels: _Set[int] = set()

    def configure(
        self,
        channel: int,
        function: str,
        frequency_hz: float,
        amplitude_v: float,
        offset_v: float = 0.0,
        phase_deg: float = 0.0,
        symmetry: float = 50.0,
        run_time_s: float | None = None,
    ) -> dict[str, Any]:
        if function not in _VALID_FUNCTIONS:
            raise ValueError(f"function must be one of {sorted(_VALID_FUNCTIONS)}, got {function!r}")
        pin = _CHANNEL_TO_PIN[channel]
        prior_channels = _Set(self._configured_channels)
        prior_amplitude = self._amplitude.get(channel)
        new_pins = sorted(_CHANNEL_TO_PIN[c] for c in (prior_channels | {channel}))
        self.device.allocator.claim("awg", new_pins)
        self._configured_channels.discard(channel)
        self._amplitude.pop(channel, None)
        try:
            self.device.backend.awg_configure(
                channel=channel,
                function=function,
                freq_hz=frequency_hz,
                amplitude_v=amplitude_v,
                offset_v=offset_v,
                phase_deg=phase_deg,
                symmetry=symmetry,
                run_time_s=run_time_s,
            )
        except Exception:
            if prior_channels:
                prior_pins = sorted(_CHANNEL_TO_PIN[c] for c in prior_channels)
                self.device.allocator.claim("awg", prior_pins)
            else:
                self.device.allocator.release("awg")
            if prior_amplitude is not None:
                self._amplitude[channel] = prior_amplitude
            self._configured_channels = prior_channels
            raise
        self._configured_channels.add(channel)
        self._amplitude[channel] = amplitude_v
        return {"configured": True, "channel": channel, "pin": pin}

    def upload_custom(
        self,
        channel: int,
        samples_npy_path: str | None,
        _samples: np.ndarray | None = None,  # for unit testing without a file
    ) -> dict[str, Any]:
        if _samples is not None:
            samples = _samples
        else:
            if samples_npy_path is None:
                raise ValueError("samples_npy_path required")
            samples = np.load(samples_npy_path)
        if samples.ndim != 1:
            raise ValueError(f"samples must be 1-D, got shape {samples.shape}")
        samples = np.asarray(samples, dtype=np.float64)
        pin = _CHANNEL_TO_PIN[channel]
        prior_channels = _Set(self._configured_channels)
        new_pins = sorted(_CHANNEL_TO_PIN[c] for c in (prior_channels | {channel}))
        self.device.allocator.claim("awg", new_pins)
        try:
            self.device.backend.awg_upload_custom(channel=channel, samples=samples)
        except Exception:
            if prior_channels:
                prior_pins = sorted(_CHANNEL_TO_PIN[c] for c in prior_channels)
                self.device.allocator.claim("awg", prior_pins)
            else:
                self.device.allocator.release("awg")
            raise
        self._configured_channels.add(channel)
        return {"uploaded": True, "channel": channel, "n_samples": len(samples), "pin": pin}

    def start(self, channel: int) -> dict[str, Any]:
        if channel not in self._configured_channels:
            raise InstrumentNotConfigured(
                f"awg.configure or awg.upload_custom must be called for channel {channel} before start"
            )
        self.device.gate_output("awg_start", channel=channel, amplitude=self._amplitude.get(channel, 0.0))
        self.device.backend.awg_start(channel=channel)
        return {"started": True, "channel": channel}

    def stop(self, channel: int) -> dict[str, Any]:
        self.device.backend.awg_stop(channel=channel)
        return {"stopped": True, "channel": channel}

    def release(self) -> None:
        for ch in list(self._configured_channels):
            try:
                self.device.backend.awg_stop(channel=ch)
            except Exception:
                pass
        self.device.allocator.release("awg")
        self._configured_channels.clear()
        self._amplitude.clear()
```

- [ ] **Step 4: Run AWG tests**

```bash
.venv/bin/pytest tests/unit/test_awg.py -v
```

Expected: most pass. The `upload_custom` test uses a `_samples` backdoor parameter; all shape/claim tests should pass.

- [ ] **Step 5: Fix any failures, then add pydwf AWG backend**

Add to `PydwfBackend` in `src/dwf_mcp/backends/pydwf_backend.py`, after the I2C section:

```python
    # --- AWG (AnalogOut) ----------------------------------------------------

    @property
    def _analog_out(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.analogOut

    def awg_configure(
        self, channel: int, function: str, freq_hz: float,
        amplitude_v: float, offset_v: float, phase_deg: float,
        symmetry: float, run_time_s: float | None,
    ) -> None:
        from pydwf import DwfAnalogOutFunction, DwfAnalogOutNode  # type: ignore[import-untyped]
        ch_idx = channel - 1
        ao = self._analog_out
        node = DwfAnalogOutNode.Carrier
        func_map = {
            "Sine":     DwfAnalogOutFunction.Sine,
            "Square":   DwfAnalogOutFunction.Square,
            "Triangle": DwfAnalogOutFunction.Triangle,
            "RampUp":   DwfAnalogOutFunction.RampUp,
            "RampDown": DwfAnalogOutFunction.RampDown,
            "DC":       DwfAnalogOutFunction.DC,
            "Noise":    DwfAnalogOutFunction.Noise,
            "Custom":   DwfAnalogOutFunction.Custom,
        }
        ao.nodeEnableSet(ch_idx, node, True)
        ao.nodeFunctionSet(ch_idx, node, func_map[function])
        ao.nodeFrequencySet(ch_idx, node, freq_hz)
        ao.nodeAmplitudeSet(ch_idx, node, amplitude_v)
        ao.nodeOffsetSet(ch_idx, node, offset_v)
        ao.nodePhaseSet(ch_idx, node, phase_deg)
        ao.nodeSymmetrySet(ch_idx, node, symmetry)
        ao.runSet(ch_idx, run_time_s if run_time_s is not None else 0.0)
        # Apply params to hardware without starting output.
        ao.configure(ch_idx, False)

    def awg_upload_custom(self, channel: int, samples: np.ndarray) -> None:
        from pydwf import DwfAnalogOutNode  # type: ignore[import-untyped]
        ch_idx = channel - 1
        ao = self._analog_out
        node = DwfAnalogOutNode.Carrier
        ao.nodeEnableSet(ch_idx, node, True)
        ao.nodeDataSet(ch_idx, node, samples.tolist())
        ao.configure(ch_idx, False)

    def awg_start(self, channel: int) -> None:
        self._analog_out.configure(channel - 1, True)

    def awg_stop(self, channel: int) -> None:
        self._analog_out.configure(channel - 1, False)
```

- [ ] **Step 6: Run full unit tests**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/awg.py src/dwf_mcp/backends/pydwf_backend.py tests/unit/test_awg.py
git commit -m "feat: AWG instrument (configure/upload_custom/start/stop) + pydwf backend"
```

---

## Task 4: Pattern instrument + device.py `pattern_start` kind

**Files:**
- Create: `src/dwf_mcp/instruments/pattern.py`
- Modify: `src/dwf_mcp/device.py`
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/unit/test_pattern.py`

### Background

Pattern uses `device.digitalOut`. pydwf calls:
- `digitalOut.enableSet(pin_idx, True)` — enable pin
- `digitalOut.typeSet(pin_idx, DwfDigitalOutType.Pulse)` — or Clock, Random, Custom
- `digitalOut.frequencySet(pin_idx, freq_hz)`
- `digitalOut.dutyCycleSet(pin_idx, duty)` — 0.0–1.0
- `digitalOut.idleSet(pin_idx, DwfDigitalOutIdle.Low)` — or High, Init (= Hi-Z on AD3)
- `digitalOut.configure(True)` — start all enabled outputs
- `digitalOut.configure(False)` — stop (does not release pin resources in pydwf)

DIO pin `dioN` → pydwf index N. Per-pin accumulating claim model: `pattern.configure(pin="dio0")` claims `["dio0"]`; adding `"dio1"` claims `["dio0", "dio1"]`. `pattern.stop(pin)` stops output but does not release claim.

`pattern_voltage` safety check: AD3 DIO is fixed 3.3V. If `policy.pattern_voltage` is not `"3.3"` or `3.3`, raise `SafetyViolation`.

- [ ] **Step 1: Write failing pattern unit tests**

Create `tests/unit/test_pattern.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.pattern import Pattern
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(pattern_voltage="3.3"),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def pattern(device: DwfDevice, tmp_path: Path) -> Pattern:
    device.open()
    return Pattern(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pin(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    assert "dio0" in pattern.device.allocator.claimed_pins()


def test_configure_accumulates_pins(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.configure(pin="dio1", function="Clock", frequency_hz=500.0, duty=0.5, idle_state="low")
    pins = pattern.device.allocator.claimed_pins()
    assert "dio0" in pins and "dio1" in pins


def test_start_safety_gate_wrong_voltage_raises(tmp_path: Path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(pattern_voltage="5.0"),  # wrong voltage for AD3
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    p = Pattern(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
    p.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    with pytest.raises(SafetyViolation, match="3.3"):
        p.start(pin="dio0")


def test_start_calls_backend_start(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.start(pin="dio0")
    fake: FakeBackend = pattern.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.pattern_calls if c[0] == "start"]
    assert len(starts) == 1
    assert starts[0][1]["pin_idx"] == 0


def test_stop_does_not_release_claim(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.stop(pin="dio0")
    assert "dio0" in pattern.device.allocator.claimed_pins()


def test_release_clears_all_claims(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.configure(pin="dio1", function="Clock", frequency_hz=500.0, duty=0.5, idle_state="low")
    pattern.release()
    assert pattern.device.allocator.claimed_pins() == {}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_pattern.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'dwf_mcp.instruments.pattern'`

- [ ] **Step 3: Add `check_pattern_voltage` to `policy.py`**

Edit `src/dwf_mcp/policy.py`, add after `check_awg_amplitude`:

```python
    def check_pattern_voltage(self) -> None:
        allowed = {"3.3", 3.3}
        if self.pattern_voltage not in allowed and float(self.pattern_voltage) != 3.3:
            raise SafetyViolation(
                f"AD3 DIO is fixed at 3.3 V; policy.pattern_voltage={self.pattern_voltage!r} "
                f"cannot be satisfied by hardware"
            )
```

- [ ] **Step 4: Add `pattern_start` kind to `device.py` `_check_policy`**

Edit `src/dwf_mcp/device.py`, in `_check_policy`, add a new `elif` before the final comment:

```python
        elif kind == "pattern_start":
            self.policy.check_pattern_voltage()
```

- [ ] **Step 5: Create `src/dwf_mcp/instruments/pattern.py`**

```python
"""Pattern (DigitalOut) instrument. Per-pin accumulating claim model."""
from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_Set = set

_VALID_FUNCTIONS = frozenset({"Pulse", "Clock", "Random", "Custom"})
_VALID_IDLE = frozenset({"low", "high", "hiz"})

PATTERN_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin", "function", "frequency_hz", "duty", "idle_state"],
    "properties": {
        "pin": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
        "function": {"type": "string", "enum": sorted(_VALID_FUNCTIONS)},
        "frequency_hz": {"type": "number", "minimum": 0.0},
        "duty": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "idle_state": {"type": "string", "enum": sorted(_VALID_IDLE)},
    },
}

PATTERN_PIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin"],
    "properties": {"pin": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"}},
}


def _pin_idx(pin: str) -> int:
    return int(pin[3:])


class Pattern(Instrument):
    name = "pattern"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", PATTERN_CONFIGURE_SCHEMA),
        "start":     ("start",     PATTERN_PIN_SCHEMA),
        "stop":      ("stop",      PATTERN_PIN_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured_pins: _Set[str] = set()

    def configure(
        self,
        pin: str,
        function: str,
        frequency_hz: float,
        duty: float,
        idle_state: str,
    ) -> dict[str, Any]:
        if function not in _VALID_FUNCTIONS:
            raise ValueError(f"function must be one of {sorted(_VALID_FUNCTIONS)}, got {function!r}")
        if idle_state not in _VALID_IDLE:
            raise ValueError(f"idle_state must be one of {sorted(_VALID_IDLE)}, got {idle_state!r}")
        prior_pins = _Set(self._configured_pins)
        new_pins = sorted(prior_pins | {pin})
        self.device.allocator.claim("pattern", new_pins)
        self._configured_pins.discard(pin)
        try:
            self.device.backend.pattern_configure(
                pin_idx=_pin_idx(pin),
                function=function,
                freq_hz=frequency_hz,
                duty=duty,
                idle_state=idle_state,
            )
        except Exception:
            if prior_pins:
                self.device.allocator.claim("pattern", sorted(prior_pins))
            else:
                self.device.allocator.release("pattern")
            self._configured_pins = prior_pins
            raise
        self._configured_pins.add(pin)
        return {"configured": True, "pin": pin}

    def start(self, pin: str) -> dict[str, Any]:
        if pin not in self._configured_pins:
            raise InstrumentNotConfigured(
                f"pattern.configure must be called for {pin!r} before start"
            )
        self.device.gate_output("pattern_start", pin=pin, voltage=self.device.policy.pattern_voltage)
        self.device.backend.pattern_start(pin_idx=_pin_idx(pin))
        return {"started": True, "pin": pin}

    def stop(self, pin: str) -> dict[str, Any]:
        self.device.backend.pattern_stop(pin_idx=_pin_idx(pin))
        return {"stopped": True, "pin": pin}

    def release(self) -> None:
        for pin in list(self._configured_pins):
            try:
                self.device.backend.pattern_stop(pin_idx=_pin_idx(pin))
            except Exception:
                pass
        self.device.allocator.release("pattern")
        self._configured_pins.clear()
```

- [ ] **Step 6: Add pydwf Pattern backend methods**

Add to `PydwfBackend` in `src/dwf_mcp/backends/pydwf_backend.py`:

```python
    # --- Pattern (DigitalOut) -----------------------------------------------

    @property
    def _digital_out(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalOut

    def pattern_configure(
        self, pin_idx: int, function: str, freq_hz: float,
        duty: float, idle_state: str,
    ) -> None:
        from pydwf import (  # type: ignore[import-untyped]
            DwfDigitalOutIdle, DwfDigitalOutType,
        )
        dout = self._digital_out
        type_map = {
            "Pulse":  DwfDigitalOutType.Pulse,
            "Clock":  DwfDigitalOutType.Clock,
            "Random": DwfDigitalOutType.Random,
            "Custom": DwfDigitalOutType.Custom,
        }
        idle_map = {
            "low":  DwfDigitalOutIdle.Low,
            "high": DwfDigitalOutIdle.High,
            "hiz":  DwfDigitalOutIdle.Init,  # Init = Hi-Z on AD3
        }
        dout.enableSet(pin_idx, True)
        dout.typeSet(pin_idx, type_map[function])
        dout.frequencySet(pin_idx, freq_hz)
        dout.dutyCycleSet(pin_idx, duty)
        dout.idleSet(pin_idx, idle_map[idle_state])

    def pattern_start(self, pin_idx: int) -> None:
        self._digital_out.configure(True)

    def pattern_stop(self, pin_idx: int) -> None:
        self._digital_out.enableSet(pin_idx, False)
        self._digital_out.configure(False)
```

- [ ] **Step 7: Run pattern tests**

```bash
.venv/bin/pytest tests/unit/test_pattern.py -v
```

Expected: PASS

- [ ] **Step 8: Run full unit suite**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/dwf_mcp/instruments/pattern.py src/dwf_mcp/backends/pydwf_backend.py \
        src/dwf_mcp/device.py src/dwf_mcp/policy.py tests/unit/test_pattern.py
git commit -m "feat: Pattern instrument (configure/start/stop) + pattern_start safety gate"
```

---

## Task 5: DIO instrument

**Files:**
- Create: `src/dwf_mcp/instruments/dio.py`
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/unit/test_dio.py`

### Background

DIO uses `device.digitalIO`. pydwf calls:
- `digitalIO.outputEnableSet(mask)` — sets direction for all 16 pins at once via bitmask
- `digitalIO.outputSet(mask)` — sets output values for all pins
- `digitalIO.inputStatus()` — returns uint32 bitmask of current pin input values

Since the pydwf API is bitmask-based, the backend works per-pin by read-modify-write on the current mask. DIO direction is **purely local** (`_directions` dict on the instrument). Hardware direction register is written only inside `dio.set`/`dio.read` after the pin claim succeeds. Default direction is `"in"` (safe).

Transient claim model: claim pin → do operation → release. If pin is held by another instrument, `PinAllocationError` is raised before touching hardware.

- [ ] **Step 1: Write failing DIO unit tests**

Create `tests/unit/test_dio.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator, PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.dio import DIO


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=__import__("dwf_mcp.policy", fromlist=["SafetyPolicy"]).SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def dio(device: DwfDevice, tmp_path: Path) -> DIO:
    device.open()
    return DIO(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_default_direction_is_in(dio: DIO) -> None:
    assert dio._directions.get("dio0", "in") == "in"


def test_set_direction_does_not_touch_hardware(dio: DIO) -> None:
    dio.set_direction(pin="dio0", direction="out")
    fake: FakeBackend = dio.device.backend  # type: ignore[assignment]
    assert fake.dio_calls == []


def test_set_on_in_pin_raises_before_claim(dio: DIO) -> None:
    # Default direction is "in"; set should raise ValueError before claiming.
    with pytest.raises(ValueError, match="direction"):
        dio.set(pin="dio0", state=1)
    assert dio.device.allocator.claimed_pins() == {}


def test_set_writes_hardware_and_releases_claim(dio: DIO) -> None:
    dio.set_direction(pin="dio0", direction="out")
    dio.set(pin="dio0", state=1)
    # Claim must be released after the call.
    assert dio.device.allocator.claimed_pins() == {}
    # Hardware was called.
    fake: FakeBackend = dio.device.backend  # type: ignore[assignment]
    direction_calls = [c for c in fake.dio_calls if c[0] == "set_direction"]
    set_calls = [c for c in fake.dio_calls if c[0] == "set"]
    assert len(direction_calls) == 1
    assert direction_calls[0][1]["output"] is True
    assert len(set_calls) == 1
    assert set_calls[0][1]["state"] is True


def test_read_releases_claim(dio: DIO) -> None:
    result = dio.read(pin="dio0")
    assert isinstance(result, dict)
    assert dio.device.allocator.claimed_pins() == {}


def test_set_raises_pin_allocation_error_if_held(dio: DIO) -> None:
    # Claim dio0 from outside.
    dio.device.allocator.claim("scope", ["dio0"])
    dio.set_direction(pin="dio0", direction="out")
    with pytest.raises(PinAllocationError):
        dio.set(pin="dio0", state=1)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_dio.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'dwf_mcp.instruments.dio'`

- [ ] **Step 3: Create `src/dwf_mcp/instruments/dio.py`**

```python
"""DIO (DigitalIO) instrument. Transient per-call pin claim model."""
from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

_VALID_DIRECTIONS = frozenset({"in", "out"})

DIO_DIRECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin", "direction"],
    "properties": {
        "pin": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
        "direction": {"type": "string", "enum": ["in", "out"]},
    },
}

DIO_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin", "state"],
    "properties": {
        "pin": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
        "state": {"type": "integer", "enum": [0, 1]},
    },
}

DIO_PIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin"],
    "properties": {"pin": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"}},
}


def _pin_idx(pin: str) -> int:
    return int(pin[3:])


class DIO(Instrument):
    name = "dio"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "set_direction": ("set_direction", DIO_DIRECTION_SCHEMA),
        "set":           ("set",           DIO_SET_SCHEMA),
        "read":          ("read",          DIO_PIN_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._directions: dict[str, str] = {}  # default "in" if not set

    def set_direction(self, pin: str, direction: str) -> dict[str, Any]:
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        self._directions[pin] = direction
        return {"pin": pin, "direction": direction}

    def set(self, pin: str, state: int) -> dict[str, Any]:
        direction = self._directions.get(pin, "in")
        if direction != "out":
            raise ValueError(
                f"pin {pin!r} direction is {direction!r}; call set_direction(pin, 'out') first"
            )
        self.device.allocator.claim("dio", [pin])
        try:
            self.device.backend.dio_set_direction(pin_idx=_pin_idx(pin), output=True)
            self.device.backend.dio_set(pin_idx=_pin_idx(pin), state=bool(state))
        finally:
            self.device.allocator.release("dio")
        return {"pin": pin, "state": state}

    def read(self, pin: str) -> dict[str, Any]:
        self.device.allocator.claim("dio", [pin])
        try:
            direction = self._directions.get(pin, "in")
            self.device.backend.dio_set_direction(pin_idx=_pin_idx(pin), output=False)
            value = self.device.backend.dio_read(pin_idx=_pin_idx(pin))
        finally:
            self.device.allocator.release("dio")
        return {"pin": pin, "state": int(value), "direction": direction}

    def release(self) -> None:
        self.device.allocator.release("dio")
        self._directions.clear()
```

- [ ] **Step 4: Add pydwf DIO backend methods**

Add to `PydwfBackend` in `src/dwf_mcp/backends/pydwf_backend.py`:

```python
    # --- DIO (DigitalIO) ----------------------------------------------------

    @property
    def _digital_io(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalIO

    def dio_set_direction(self, pin_idx: int, output: bool) -> None:
        dio = self._digital_io
        current_mask = int(dio.outputEnableGet())
        if output:
            new_mask = current_mask | (1 << pin_idx)
        else:
            new_mask = current_mask & ~(1 << pin_idx)
        dio.outputEnableSet(new_mask)

    def dio_set(self, pin_idx: int, state: bool) -> None:
        dio = self._digital_io
        current_out = int(dio.outputGet())
        if state:
            new_out = current_out | (1 << pin_idx)
        else:
            new_out = current_out & ~(1 << pin_idx)
        dio.outputSet(new_out)

    def dio_read(self, pin_idx: int) -> bool:
        dio = self._digital_io
        dio.status()  # refresh input state
        input_mask = int(dio.inputStatus())
        return bool(input_mask & (1 << pin_idx))
```

- [ ] **Step 5: Run DIO tests**

```bash
.venv/bin/pytest tests/unit/test_dio.py -v
```

Expected: PASS

- [ ] **Step 6: Run full unit suite**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/dio.py src/dwf_mcp/backends/pydwf_backend.py tests/unit/test_dio.py
git commit -m "feat: DIO instrument (set_direction/set/read, transient claim) + pydwf backend"
```

---

## Task 6: Logic instrument — buffer-mode

**Files:**
- Create: `src/dwf_mcp/instruments/logic.py` (buffer-mode only in this task)
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/unit/test_logic.py` (buffer-mode cases)

### Background

Logic buffer-mode mirrors the Scope lifecycle exactly:
1. `logic.configure(pins, sample_rate_hz, buffer_size)` → claims pins, sets `DigitalIn` to Single acquisition mode
2. `logic.set_trigger(...)` → configures trigger
3. `logic.capture(output_path?, format?)` → arms, polls until Done, reads all 16 channels, slices to configured pins, writes artifact

pydwf `DigitalIn` calls:
- `digitalIn.acquisitionModeSet(DwfAcquisitionMode.Single)` or `Record`
- `digitalIn.dividerSet(divider)` — frequency divider from 100 MHz master clock; `divider = round(100e6 / sample_rate_hz)`
- `digitalIn.bufferSizeSet(buffer_size)`
- `digitalIn.configure(reconfigure=False, start=True)` — arm
- `digitalIn.status(readData=True)` — returns `DwfState`; check for `DwfState.Done`
- `digitalIn.statusData(channel=0, count)` — returns bytes/ints for all 16 pins packed; each sample is a 16-bit word (little-endian), bit N = DIO pin N. Access via `digitalIn.statusData2(count)` which returns a list of uint16.

**Pin mask**: `pins=["dio0","dio2"]` → `pin_mask = 0b0101 = 5`. After reading, the instrument slices columns from the 16-column array.

**Trigger source mapping** for `DigitalIn`:
- `"none"` → `DwfTriggerSource.None_`
- `"detector_digital_in"` → `DwfTriggerSource.DetectorDigitalIn`
- `"external1"` → `DwfTriggerSource.External1`
- `"external2"` → `DwfTriggerSource.External2`

- [ ] **Step 1: Write failing Logic buffer-mode tests**

Create `tests/unit/test_logic.py` (buffer-mode section):

```python
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.logic import Logic
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
def logic(device: DwfDevice, tmp_path: Path) -> Logic:
    device.open()
    return Logic(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


# --- Buffer-mode tests ---

def test_configure_claims_pins(logic: Logic) -> None:
    logic.configure(pins=["dio0", "dio1"], sample_rate_hz=1_000_000, buffer_size=1024)
    claimed = logic.device.allocator.claimed_pins()
    assert "dio0" in claimed and "dio1" in claimed


def test_configure_calls_backend(logic: Logic) -> None:
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    cfgs = [c for c in fake.logic_calls if c[0] == "configure"]
    assert len(cfgs) == 1
    assert cfgs[0][1]["pin_mask"] == 0b1  # dio0 = bit 0
    assert cfgs[0][1]["buffer_size"] == 1024


def test_configure_partial_failure_releases_claim(
    logic: Logic, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("backend exploded")
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    monkeypatch.setattr(fake, "logic_configure", boom)
    with pytest.raises(RuntimeError):
        logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    assert logic.device.allocator.claimed_pins() == {}


def test_capture_writes_npz_artifact(logic: Logic, tmp_path: Path) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Provide canned data: 1024 samples, 16 channels, dio0 = 1
    data = np.zeros((1024, 16), dtype=np.uint8)
    data[:, 0] = 1
    fake._logic_canned_data = data
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    out_path = tmp_path / "logic_test.npz"
    result = logic.capture(output_path=str(out_path))
    assert "path" in result
    assert Path(result["path"]).exists()
    loaded = np.load(result["path"])
    # Should have 'dio0' key with shape (1024,)
    assert "dio0" in loaded
    assert loaded["dio0"].shape == (1024,)
    assert all(loaded["dio0"] == 1)


def test_capture_releases_claim_after_done(logic: Logic) -> None:
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    logic.capture()
    # Claim is held until instrument.release() — NOT released after capture.
    # (Same behavior as scope: claim lives for the lifetime of the configure.)
    assert "dio0" in logic.device.allocator.claimed_pins()


def test_capture_invokes_vcd_writer(logic: Logic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dwf_mcp.vcd_writer as vw
    calls = []
    def fake_write(path, samples, pin_names, sample_rate_hz):
        calls.append((path, samples, pin_names, sample_rate_hz))
    monkeypatch.setattr(vw, "write", fake_write)
    monkeypatch.setattr(vw, "HAS_VCD", True)
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    fake._logic_canned_data = np.zeros((64, 16), dtype=np.uint8)
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=64)
    logic.capture(output_path=str(tmp_path / "out.vcd"), format="vcd")
    assert len(calls) == 1
    assert calls[0][2] == ["dio0"]


def test_capture_vcd_missing_package_raises(logic: Logic, monkeypatch: pytest.MonkeyPatch) -> None:
    import dwf_mcp.vcd_writer as vw
    monkeypatch.setattr(vw, "HAS_VCD", False)
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=64)
    with pytest.raises(ImportError, match="pyvcd"):
        logic.capture(format="vcd")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_logic.py -v -k "not record"
```

Expected: FAIL — `ModuleNotFoundError: No module named 'dwf_mcp.instruments.logic'`

- [ ] **Step 3: Create `src/dwf_mcp/instruments/logic.py` (buffer-mode only)**

```python
"""Logic (DigitalIn) instrument: buffer-mode capture and streaming record."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp import vcd_writer
from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

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


@dataclasses.dataclass
class _RecordingSession:
    record_id: str
    task: asyncio.Task  # type: ignore[type-arg]
    queue: asyncio.Queue  # type: ignore[type-arg]  # streaming seam for future MCP notifications
    chunks: list[np.ndarray]
    pins: list[str]
    sample_rate_hz: float
    output_path: str | None
    format: str
    lost_samples: int
    done: bool
    error: str | None


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
        self._sessions: dict[str, _RecordingSession] = {}

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
        if format == "vcd" and not vcd_writer.HAS_VCD:
            raise ImportError(
                "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
            )
        cfg = self._config
        self.device.backend.logic_arm()
        deadline = time.monotonic() + max(
            cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0
        )
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
            return {"path": str(path), "format": "vcd", "n_samples": len(samples)}

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
        return {"path": result.path, "sidecar_path": result.sidecar_path, "format": "npz"}

    # --- Streaming (record) — stubs filled in Task 7 ---

    async def record_start(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("record_start is added in Task 7")

    def record_status(self, record_id: str) -> dict[str, Any]:
        raise NotImplementedError("record_status is added in Task 7")

    async def record_stop(self, record_id: str) -> dict[str, Any]:
        raise NotImplementedError("record_stop is added in Task 7")

    def release(self) -> None:
        for session in list(self._sessions.values()):
            session.task.cancel()
        self._sessions.clear()
        self.device.allocator.release("logic")
        self._config = None
```

- [ ] **Step 4: Add pydwf Logic buffer-mode backend methods**

Add to `PydwfBackend` in `src/dwf_mcp/backends/pydwf_backend.py`:

```python
    # --- Logic buffer-mode (DigitalIn) --------------------------------------

    @property
    def _digital_in(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalIn

    def logic_configure(
        self, pin_mask: int, sample_rate_hz: float, buffer_size: int
    ) -> None:
        from pydwf import DwfAcquisitionMode  # type: ignore[import-untyped]
        din = self._digital_in
        # DigitalIn clock is 100 MHz; divider gives the actual sample rate.
        divider = max(1, round(100_000_000 / sample_rate_hz))
        din.dividerSet(divider)
        din.bufferSizeSet(buffer_size)
        din.acquisitionModeSet(DwfAcquisitionMode.Single)

    def logic_set_trigger(
        self, source: str, pin_idx: int | None, level: float | None,
        condition: str | None, position_s: float | None, timeout_s: float | None,
    ) -> None:
        from pydwf import DwfTriggerSource  # type: ignore[import-untyped]
        din = self._digital_in
        src_map = {
            "none":                 DwfTriggerSource.None_,
            "detector_digital_in":  DwfTriggerSource.DetectorDigitalIn,
            "external1":            DwfTriggerSource.External1,
            "external2":            DwfTriggerSource.External2,
        }
        din.triggerSourceSet(src_map[source])
        if position_s is not None:
            din.triggerPositionSet(position_s)
        if timeout_s is not None:
            din.triggerAutoTimeoutSet(timeout_s)

    def logic_arm(self) -> None:
        self._digital_in.configure(False, True)

    def logic_status(self) -> str:
        st = self._digital_in.status(True)
        if st == __import__("pydwf", fromlist=["DwfState"]).DwfState.Done:
            return "Done"
        return str(getattr(st, "name", st))

    def logic_read(self, count: int) -> np.ndarray:
        # statusData2 returns a list of uint16, one per sample (all 16 channels packed).
        raw = self._digital_in.statusData2(count)
        arr = np.array(raw, dtype=np.uint16)
        # Unpack to (count, 16) uint8 array: bit N of sample = pin N.
        result = np.zeros((len(arr), 16), dtype=np.uint8)
        for bit in range(16):
            result[:, bit] = (arr >> bit) & 1
        return result
```

- [ ] **Step 5: Run Logic buffer-mode tests**

```bash
.venv/bin/pytest tests/unit/test_logic.py -v -k "not record"
```

Expected: PASS for all buffer-mode tests.

- [ ] **Step 6: Run full unit suite**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/logic.py src/dwf_mcp/backends/pydwf_backend.py tests/unit/test_logic.py
git commit -m "feat: Logic instrument buffer-mode (configure/set_trigger/capture) + pydwf backend"
```

---

## Task 7: Logic streaming (`record_start` / `record_status` / `record_stop`)

**Files:**
- Modify: `src/dwf_mcp/instruments/logic.py` (fill in the 3 streaming stubs)
- Modify: `src/dwf_mcp/backends/pydwf_backend.py` (add record-mode methods)
- Modify: `tests/unit/test_logic.py` (add streaming tests)

### Background

`record_start` and `record_stop` are `async def` — they schedule/cancel `asyncio.Task`s. `record_status` is sync (dict lookup). The background polling loop is a coroutine method on `Logic`. FakeBackend's `logic_record_status` returns the first element of `_logic_record_status_sequence`; the loop exits when `remaining == 0`.

- [ ] **Step 1: Add streaming test cases to `tests/unit/test_logic.py`**

Append to the existing `tests/unit/test_logic.py` file:

```python
# --- Streaming (record) tests ---

@pytest.mark.asyncio
async def test_record_start_returns_record_id(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Single poll: 10 available, 0 lost, 0 remaining → loop exits after one iteration.
    fake.set_logic_record_status_sequence([(10, 0, 0)])
    result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    assert "record_id" in result
    assert isinstance(result["record_id"], str)
    # Clean up.
    await logic.record_stop(record_id=result["record_id"])


@pytest.mark.asyncio
async def test_record_status_reports_done(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    fake.set_logic_record_status_sequence([(5, 0, 0)])
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.005
    )
    rid = start_result["record_id"]
    # Give the background task time to run.
    await asyncio.sleep(0.05)
    status = logic.record_status(record_id=rid)
    assert status["record_id"] == rid
    assert "done" in status
    await logic.record_stop(record_id=rid)


@pytest.mark.asyncio
async def test_record_stop_writes_artifact(logic: Logic, tmp_path: Path) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Provide canned chunk data.
    fake._logic_record_canned_chunk = np.zeros((10, 16), dtype=np.uint8)
    fake._logic_record_canned_chunk[:, 0] = 1  # dio0 high
    fake.set_logic_record_status_sequence([(10, 0, 0)])
    out_path = tmp_path / "rec.npz"
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01,
        output_path=str(out_path),
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.05)
    stop_result = await logic.record_stop(record_id=rid)
    assert stop_result["error"] is None
    assert stop_result["artifact_path"] is not None
    assert Path(stop_result["artifact_path"]).exists()


@pytest.mark.asyncio
async def test_record_lost_samples_counted(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Sequence: first poll has 5 available + 3 lost, then 5 available + 0 lost + remaining=0.
    fake.set_logic_record_status_sequence([(5, 3, 1), (5, 0, 0)])
    fake._logic_record_canned_chunk = np.zeros((10, 16), dtype=np.uint8)
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.1)
    stop_result = await logic.record_stop(record_id=rid)
    assert stop_result["lost_samples"] >= 3


@pytest.mark.asyncio
async def test_record_backend_exception_sets_error(
    logic: Logic, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    call_count = [0]
    def boom_on_second(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise RuntimeError("simulated backend failure")
        return (5, 0, 1)  # remaining=1 to keep loop running
    monkeypatch.setattr(fake, "logic_record_status", boom_on_second)
    fake._logic_record_canned_chunk = np.zeros((5, 16), dtype=np.uint8)
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.1)
    stop_result = await logic.record_stop(record_id=rid)
    assert stop_result["error"] is not None


@pytest.mark.asyncio
async def test_record_claims_released_after_stop(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    fake.set_logic_record_status_sequence([(10, 0, 0)])
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.05)
    await logic.record_stop(record_id=rid)
    assert logic.device.allocator.claimed_pins() == {}
```

- [ ] **Step 2: Run new streaming tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_logic.py -v -k "record"
```

Expected: FAIL with `NotImplementedError: record_start is added in Task 7`

- [ ] **Step 3: Implement streaming in `logic.py`**

Replace the three streaming stubs in `Logic` with the full implementation:

```python
    async def record_start(
        self,
        pins: list[str],
        sample_rate_hz: float,
        duration_s: float,
        output_path: str | None = None,
        format: str = "npz",
    ) -> dict[str, Any]:
        if format not in _VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(_VALID_FORMATS)}, got {format!r}")
        if format == "vcd" and not vcd_writer.HAS_VCD:
            raise ImportError(
                "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
            )
        self.device.allocator.claim("logic", pins)
        try:
            self.device.backend.logic_record_configure(
                pin_mask=_pins_to_mask(pins),
                sample_rate_hz=sample_rate_hz,
            )
            self.device.backend.logic_record_arm()
        except Exception:
            self.device.allocator.release("logic")
            raise
        record_id = str(uuid.uuid4())
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        session = _RecordingSession(
            record_id=record_id,
            task=None,  # type: ignore[arg-type]  — filled below
            queue=queue,
            chunks=[],
            pins=list(pins),
            sample_rate_hz=sample_rate_hz,
            output_path=output_path,
            format=format,
            lost_samples=0,
            done=False,
            error=None,
        )
        session.task = asyncio.create_task(self._record_loop(session))
        self._sessions[record_id] = session
        return {"record_id": record_id}

    async def _record_loop(self, session: _RecordingSession) -> None:
        try:
            while not session.done:
                await asyncio.sleep(0.010)
                available, lost, remaining = self.device.backend.logic_record_status()
                session.lost_samples += lost
                if available > 0:
                    chunk = self.device.backend.logic_record_read(available)
                    session.chunks.append(chunk)
                    await session.queue.put(chunk)
                if remaining == 0:
                    session.done = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.error = str(exc)
            session.done = True

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
        # 1. Cancel the background task.
        session.task.cancel()
        with suppress(asyncio.CancelledError):
            await session.task
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
                session.chunks.append(chunk)
        except Exception as exc:
            log.warning("drain after record_stop failed: %s", exc)
        # 4. Write artifact (best-effort).
        artifact_path: str | None = None
        artifact_error: str | None = None
        if session.chunks:
            try:
                all_raw = np.concatenate(session.chunks, axis=0)
                pin_indices = _pin_indices(session.pins)
                samples = all_raw[:, pin_indices].astype(np.uint8)
                result_dict = self._write_artifact(
                    samples=samples,
                    pin_names=session.pins,
                    sample_rate_hz=session.sample_rate_hz,
                    output_path=session.output_path,
                    format=session.format,
                )
                artifact_path = result_dict.get("path")
            except Exception as exc:
                log.exception("artifact write failed for record_id=%r", record_id)
                artifact_error = str(exc)
        # 5. Remove session.
        del self._sessions[record_id]
        # 6. Release pin claim.
        self.device.allocator.release("logic")
        return {
            "record_id": record_id,
            "artifact_path": artifact_path,
            "lost_samples": session.lost_samples,
            "error": session.error,
            "artifact_error": artifact_error,
        }
```

- [ ] **Step 4: Add pydwf Logic record-mode backend methods**

Add to `PydwfBackend` in `src/dwf_mcp/backends/pydwf_backend.py`:

```python
    # --- Logic record-mode (DigitalIn streaming) ----------------------------

    def logic_record_configure(self, pin_mask: int, sample_rate_hz: float) -> None:
        from pydwf import DwfAcquisitionMode  # type: ignore[import-untyped]
        din = self._digital_in
        divider = max(1, round(100_000_000 / sample_rate_hz))
        din.dividerSet(divider)
        din.acquisitionModeSet(DwfAcquisitionMode.Record)

    def logic_record_arm(self) -> None:
        self._digital_in.configure(False, True)

    def logic_record_status(self) -> tuple[int, int, int]:
        din = self._digital_in
        din.status(True)
        available = din.statusRecordProgress()[0]  # returns (available, lost, remaining)
        # pydwf statusRecord returns (available, lost, remaining)
        return tuple(din.statusRecord())  # type: ignore[return-value]

    def logic_record_read(self, count: int) -> np.ndarray:
        raw = self._digital_in.statusData2(count)
        arr = np.array(raw, dtype=np.uint16)
        result = np.zeros((len(arr), 16), dtype=np.uint8)
        for bit in range(16):
            result[:, bit] = (arr >> bit) & 1
        return result

    def logic_record_stop(self) -> None:
        self._digital_in.configure(False, False)
```

- [ ] **Step 5: Run all Logic tests**

```bash
.venv/bin/pytest tests/unit/test_logic.py -v
```

Expected: all pass (buffer-mode + streaming).

- [ ] **Step 6: Run full unit suite**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```

Expected: all pass. Count ~175+.

- [ ] **Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/logic.py src/dwf_mcp/backends/pydwf_backend.py tests/unit/test_logic.py
git commit -m "feat: Logic instrument streaming (record_start/status/stop) + pydwf backend"
```

---

## Task 8: Server registration + final wiring

**Files:**
- Modify: `src/dwf_mcp/server.py`

### Background

`build_app` currently registers Scope, Supply, and I2C. Stage 3a adds AWG, Pattern, DIO, and Logic. All four new instruments follow the same `register_instrument(Cls)` pattern.

- [ ] **Step 1: Write a registration smoke test**

Append to `tests/unit/test_server_async.py`:

```python
def test_build_app_registers_stage3a_tools(tmp_path):
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    tool_names = set(app._tools)
    expected = {
        "awg.configure", "awg.upload_custom", "awg.start", "awg.stop",
        "pattern.configure", "pattern.start", "pattern.stop",
        "dio.set_direction", "dio.set", "dio.read",
        "logic.configure", "logic.set_trigger", "logic.capture",
        "logic.record_start", "logic.record_status", "logic.record_stop",
    }
    missing = expected - tool_names
    assert missing == set(), f"missing tools: {missing}"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
.venv/bin/pytest tests/unit/test_server_async.py::test_build_app_registers_stage3a_tools -v
```

Expected: FAIL — tools not registered yet.

- [ ] **Step 3: Update `build_app` in `server.py`**

Edit the imports at the top of `server.py` (add the 4 new instruments after the existing I2C import):

```python
from dwf_mcp.instruments.awg import AWG
from dwf_mcp.instruments.dio import DIO
from dwf_mcp.instruments.i2c import I2C
from dwf_mcp.instruments.logic import Logic
from dwf_mcp.instruments.pattern import Pattern
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.instruments.supply import Supply
```

Edit `build_app` to register the new instruments (after the existing `app.register_instrument(I2C)` line):

```python
    app.register_instrument(AWG)
    app.register_instrument(Pattern)
    app.register_instrument(DIO)
    app.register_instrument(Logic)
```

- [ ] **Step 4: Run the registration test**

```bash
.venv/bin/pytest tests/unit/test_server_async.py -v
```

Expected: PASS (both tests).

- [ ] **Step 5: Run full unit suite — final check**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```

Expected: all pass. Tool count from `waveforms.status` call should show 29 registered tools (13 existing + 16 new).

- [ ] **Step 6: Commit**

```bash
git add src/dwf_mcp/server.py tests/unit/test_server_async.py
git commit -m "feat: register AWG, Pattern, DIO, Logic instruments in build_app (stage 3a complete)"
```

---

## Task 9: Hardware smoke tests

**Files:**
- Create: `tests/hardware/test_awg_hardware.py`
- Create: `tests/hardware/test_logic_hardware.py`
- Create: `tests/hardware/test_dio_hardware.py`

### Wiring required

- AWG: W1 → scope ch1+ (same wire used by existing scope hardware test)
- Logic + Pattern: DIO0 → DIO1 loopback (pattern drives DIO0, logic captures DIO1)
- DIO: DIO0 out, DIO1 in, loopback wire DIO0 → DIO1

Run all hardware tests with: `pytest tests/hardware/ -m hardware -v`

- [ ] **Step 1: Create `tests/hardware/test_awg_hardware.py`**

```python
"""Hardware smoke test for AWG.

Wiring: W1 → scope ch1+ (same wire as existing scope hardware test).
Run: pytest tests/hardware/test_awg_hardware.py -m hardware -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.hardware
def test_awg_sine_captured_by_scope(tmp_path: Path) -> None:
    pytest.importorskip("pydwf")

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.scope import Scope
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    try:
        arts = ArtifactWriter(workspace=tmp_path)
        awg = AWG(device=device, artifacts=arts)
        scope = Scope(device=device, artifacts=arts)

        awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
        awg.start(channel=1)

        scope.configure(channels=[1], range_v=5.0, sample_rate_hz=100_000, buffer_size=4096)
        scope.set_trigger(
            source="detector_analog_in", channel=1, level_v=0.0,
            condition="Rising", timeout_s=2.0,
        )
        result = scope.capture()
        freq = result["summary"]["ch1"]["freq_estimate"]
        assert 900 < freq < 1100, f"expected ~1000 Hz, got {freq}"
    finally:
        device.close()
```

- [ ] **Step 2: Create `tests/hardware/test_logic_hardware.py`**

```python
"""Hardware smoke test for Logic and Pattern.

Wiring: DIO0 → DIO1 loopback (pattern drives DIO0, logic captures DIO1).
Run: pytest tests/hardware/test_logic_hardware.py -m hardware -v
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


@pytest.mark.hardware
def test_pattern_clock_captured_by_logic(tmp_path: Path) -> None:
    pytest.importorskip("pydwf")

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.logic import Logic
    from dwf_mcp.instruments.pattern import Pattern
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    try:
        arts = ArtifactWriter(workspace=tmp_path)
        pat = Pattern(device=device, artifacts=arts)
        logic = Logic(device=device, artifacts=arts)

        # Drive DIO0 at 10 kHz clock, capture DIO1 at 1 MHz.
        pat.configure(pin="dio0", function="Clock", frequency_hz=10_000.0, duty=0.5, idle_state="low")
        pat.start(pin="dio0")

        logic.configure(pins=["dio1"], sample_rate_hz=1_000_000, buffer_size=4096)
        result = logic.capture()
        assert "path" in result
        loaded = np.load(result["path"])
        dio1 = loaded["dio1"]
        # At 1 MHz sample rate and 10 kHz clock, expect ~100 samples per period.
        # Check that dio1 has both 0 and 1 values (the clock is toggling).
        assert 1 in dio1 and 0 in dio1, "expected clock transitions on DIO1"
    finally:
        device.close()
```

- [ ] **Step 3: Create `tests/hardware/test_dio_hardware.py`**

```python
"""Hardware smoke test for DIO.

Wiring: DIO0 (out) → DIO1 (in) loopback.
Run: pytest tests/hardware/test_dio_hardware.py -m hardware -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.hardware
def test_dio_loopback_high_low(tmp_path: Path) -> None:
    pytest.importorskip("pydwf")

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.dio import DIO
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    try:
        arts = ArtifactWriter(workspace=tmp_path)
        dio = DIO(device=device, artifacts=arts)

        dio.set_direction(pin="dio0", direction="out")
        dio.set_direction(pin="dio1", direction="in")

        dio.set(pin="dio0", state=1)
        result_high = dio.read(pin="dio1")
        assert result_high["state"] == 1, f"expected DIO1=1, got {result_high['state']}"

        dio.set(pin="dio0", state=0)
        result_low = dio.read(pin="dio1")
        assert result_low["state"] == 0, f"expected DIO1=0, got {result_low['state']}"
    finally:
        device.close()
```

- [ ] **Step 4: Verify hardware tests are deselected in normal runs**

```bash
.venv/bin/pytest tests/unit/ tests/integration/ -v --tb=short -m "not hardware"
```

Expected: all unit + integration tests pass; hardware tests deselected.

- [ ] **Step 5: Run final full suite to confirm expected count**

```bash
.venv/bin/pytest tests/ -m "not hardware" -v --tb=short
```

Expected: ~185–200 passed, 8+ deselected (hardware).

- [ ] **Step 6: Commit**

```bash
git add tests/hardware/test_awg_hardware.py tests/hardware/test_logic_hardware.py \
        tests/hardware/test_dio_hardware.py
git commit -m "test: hardware smoke tests for AWG, Pattern+Logic, DIO (stage 3a)"
```

---

## Self-Review Checklist

Ran against spec (`docs/superpowers/specs/2026-06-03-stage3a-design.md`):

- [x] **AWG**: configure/upload_custom/start/stop — all 4 tools, safety gate, accumulating pin claim, rollback ✓
- [x] **Logic buffer-mode**: configure/set_trigger/capture, npz + VCD, pin mask, pin slicing ✓
- [x] **Logic streaming**: record_start/status/stop, `_RecordingSession`, `asyncio.Queue` seam, 7-step stop sequence ✓
- [x] **Pattern**: configure/start/stop, `pattern_start` safety gate, per-pin accumulating claim ✓
- [x] **DIO**: set_direction/set/read, transient claim, purely-local direction, default "in" ✓
- [x] **VCD writer**: `HAS_VCD` flag, `ImportError` message, pyvcd import as `vcd` ✓
- [x] **pyproject.toml**: `vcd = ["pyvcd>=2.0"]` optional extra + dev extra updated ✓
- [x] **server.py async fix**: `asyncio.iscoroutine` check before await ✓
- [x] **backend.py stubs**: All 19 new methods with `NotImplementedError` ✓
- [x] **FakeBackend**: All new methods with call recording + canned response helpers ✓
- [x] **`pattern_start` in device.py `_check_policy`**: calls `policy.check_pattern_voltage()` ✓
- [x] **policy.py**: `check_pattern_voltage` raises on non-3.3V ✓
- [x] **Server registration**: AWG, Pattern, DIO, Logic all registered in `build_app` ✓
- [x] **Hardware smoke tests**: 3 files, correct wiring notes, `pytest.importorskip("pydwf")` ✓
- [x] **3b seam**: `_RecordingSession.queue` present and documented ✓

Type consistency spot-check:
- `_pin_idx(pin)` used consistently in pattern.py and dio.py ✓
- `_pins_to_mask(pins)` and `_pin_indices(pins)` used in logic.py, both defined in logic.py ✓
- `awg_configure(channel, function, freq_hz, ...)` — backend signature matches instrument calls ✓
- `logic_record_status() -> tuple[int, int, int]` — matches loop destructure `available, lost, remaining` ✓
