# Stage 3b Instruments Implementation Plan: DMM, SPI, UART, CAN

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four new instruments (DMM, SPI, UART, CAN) to dwf-mcp, bringing the server from 29 to 43 tools.

**Architecture:** All four instruments follow the established `Instrument` subclass pattern — JSON schemas, `allocator.claim()` on configure, partial-failure rollback on backend exception, `allocator.release()` on `instrument.release()`. DMM is stateless (transient claim per call); SPI/UART/CAN claim pins persistently on configure. Each instrument gets backend stubs in `backend.py`, canned-response fakes in `fake.py`, real pydwf implementations in `pydwf_backend.py`, and is registered in `build_app()`.

**Tech Stack:** Python 3.12, pydwf 1.1.x, pytest, numpy

**Spec:** `docs/superpowers/specs/2026-06-03-stage3b-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/dwf_mcp/backend.py` | Modify | Add `NotImplementedError` stubs for all 15 new backend methods |
| `src/dwf_mcp/backends/fake.py` | Modify | Add FakeBackend implementations with call recording + canned responses |
| `src/dwf_mcp/backends/pydwf_backend.py` | Modify | Implement all 15 new backend methods against pydwf protocol APIs |
| `src/dwf_mcp/instruments/dmm.py` | Create | DMM: stateless `measure()` with transient AnalogIn claim |
| `src/dwf_mcp/instruments/spi.py` | Create | SPI: configure/transfer/write/read with per-operation pin validation |
| `src/dwf_mcp/instruments/uart.py` | Create | UART: configure/write/read with TX/RX pin validation |
| `src/dwf_mcp/instruments/can.py` | Create | CAN: configure/send/receive with frame validation |
| `src/dwf_mcp/server.py` | Modify | Register DMM, SPI, UART, CAN in `build_app()` |
| `tests/unit/test_dmm.py` | Create | DMM unit tests |
| `tests/unit/test_spi.py` | Create | SPI unit tests |
| `tests/unit/test_uart.py` | Create | UART unit tests |
| `tests/unit/test_can.py` | Create | CAN unit tests |
| `tests/hardware/test_dmm_hardware.py` | Create | DMM hardware smoke test |
| `tests/hardware/test_spi_hardware.py` | Create | SPI hardware smoke test |
| `tests/hardware/test_uart_hardware.py` | Create | UART hardware smoke test |
| `tests/hardware/test_can_hardware.py` | Create | CAN hardware smoke test |

---

## Task 1: Backend stubs + FakeBackend

**Files:**
- Modify: `src/dwf_mcp/backend.py`
- Modify: `src/dwf_mcp/backends/fake.py`

### Background

`backend.py` uses `raise NotImplementedError` (not `@abstractmethod`) for instrument-layer methods. `fake.py` records each call as `list[tuple[str, dict]]` and returns configurable canned responses. Pattern mirrors the existing logic record-mode block.

- [ ] **Step 1: Add backend stubs to `backend.py`**

After the `# Logic record-mode` block (line ~174), append:

```python
    # DMM (AnalogIn measurement) — added in stage 3b.
    def dmm_configure(self, channel: int, range_v: float, coupling: str, n_averages: int) -> None:
        raise NotImplementedError

    def dmm_arm(self) -> None:
        raise NotImplementedError

    def dmm_status(self) -> str:
        raise NotImplementedError

    def dmm_read(self, channel: int, count: int) -> np.ndarray:
        raise NotImplementedError

    def dmm_stop(self) -> None:
        raise NotImplementedError

    # SPI (ProtocolSPI) — added in stage 3b.
    def spi_configure(
        self, clk_idx: int, freq_hz: float, mode: int,
        mosi_idx: int | None, miso_idx: int | None, cs_idx: int | None,
        cs_polarity: str, bit_order: str,
    ) -> None:
        raise NotImplementedError

    def spi_transfer(self, data: bytes, assert_cs: bool) -> bytes:
        raise NotImplementedError

    def spi_write(self, data: bytes, assert_cs: bool) -> None:
        raise NotImplementedError

    def spi_read(self, length: int, assert_cs: bool) -> bytes:
        raise NotImplementedError

    # UART (ProtocolUART) — added in stage 3b.
    def uart_configure(
        self, baud_rate: int, tx_idx: int | None, rx_idx: int | None,
        data_bits: int, parity: str, stop_bits: int,
    ) -> None:
        raise NotImplementedError

    def uart_write(self, data: bytes) -> None:
        raise NotImplementedError

    def uart_read(self, length: int, timeout_s: float) -> tuple[bytes, bool]:
        raise NotImplementedError

    # CAN (ProtocolCAN) — added in stage 3b.
    def can_configure(self, tx_idx: int, rx_idx: int, bit_rate: int) -> None:
        raise NotImplementedError

    def can_send(self, id: int, data: bytes, extended: bool) -> None:
        raise NotImplementedError

    def can_receive(self, timeout_s: float) -> tuple[int | None, bytes, bool, int]:
        raise NotImplementedError
```

- [ ] **Step 2: Add FakeBackend state init to `fake.py` `__init__`**

After the `# Logic record-mode state` block, append to `__init__`:

```python
        # DMM (AnalogIn measurement) state
        self.dmm_calls: list[tuple[str, dict[str, Any]]] = []
        self._dmm_status_sequence: list[str] = ["Done"]
        self._dmm_status_idx = 0
        self._dmm_canned_data: dict[int, np.ndarray] = {}
        # SPI (ProtocolSPI) state
        self.spi_calls: list[tuple[str, dict[str, Any]]] = []
        self._spi_canned_rx: bytes = b""
        # UART (ProtocolUART) state
        self.uart_calls: list[tuple[str, dict[str, Any]]] = []
        self._uart_canned_rx: bytes = b""
        self._uart_parity_error: bool = False
        # CAN (ProtocolCAN) state
        self.can_calls: list[tuple[str, dict[str, Any]]] = []
        self._can_canned_frame: tuple[int | None, bytes, bool, int] = (None, b"", False, 0)
```

- [ ] **Step 3: Add FakeBackend method implementations**

After the `# Test helpers for logic` block, append:

```python
    # --- DMM (AnalogIn measurement) ---

    def dmm_configure(self, channel: int, range_v: float, coupling: str, n_averages: int) -> None:
        self.dmm_calls.append(("configure", {
            "channel": channel, "range_v": range_v,
            "coupling": coupling, "n_averages": n_averages,
        }))
        self._dmm_status_idx = 0

    def dmm_arm(self) -> None:
        self.dmm_calls.append(("arm", {}))

    def dmm_status(self) -> str:
        idx = min(self._dmm_status_idx, len(self._dmm_status_sequence) - 1)
        result = self._dmm_status_sequence[idx]
        self._dmm_status_idx += 1
        return result

    def dmm_read(self, channel: int, count: int) -> np.ndarray:
        if channel in self._dmm_canned_data:
            return self._dmm_canned_data[channel][:count]
        return np.full(count, 1.5, dtype=np.float64)

    def dmm_stop(self) -> None:
        self.dmm_calls.append(("stop", {}))

    # Test helpers
    def set_dmm_canned_data(self, channel: int, data: np.ndarray) -> None:
        self._dmm_canned_data[channel] = data

    def set_dmm_status_sequence(self, seq: list[str]) -> None:
        self._dmm_status_sequence = list(seq)
        self._dmm_status_idx = 0

    # --- SPI (ProtocolSPI) ---

    def spi_configure(
        self, clk_idx: int, freq_hz: float, mode: int,
        mosi_idx: int | None, miso_idx: int | None, cs_idx: int | None,
        cs_polarity: str, bit_order: str,
    ) -> None:
        self.spi_calls.append(("configure", {
            "clk_idx": clk_idx, "freq_hz": freq_hz, "mode": mode,
            "mosi_idx": mosi_idx, "miso_idx": miso_idx, "cs_idx": cs_idx,
            "cs_polarity": cs_polarity, "bit_order": bit_order,
        }))

    def spi_transfer(self, data: bytes, assert_cs: bool) -> bytes:
        self.spi_calls.append(("transfer", {"data": data, "assert_cs": assert_cs}))
        if self._spi_canned_rx:
            return self._spi_canned_rx[: len(data)]
        return bytes(len(data))

    def spi_write(self, data: bytes, assert_cs: bool) -> None:
        self.spi_calls.append(("write", {"data": data, "assert_cs": assert_cs}))

    def spi_read(self, length: int, assert_cs: bool) -> bytes:
        self.spi_calls.append(("read", {"length": length, "assert_cs": assert_cs}))
        if self._spi_canned_rx:
            return self._spi_canned_rx[:length]
        return bytes(length)

    # Test helper
    def set_spi_canned_rx(self, data: bytes) -> None:
        self._spi_canned_rx = data

    # --- UART (ProtocolUART) ---

    def uart_configure(
        self, baud_rate: int, tx_idx: int | None, rx_idx: int | None,
        data_bits: int, parity: str, stop_bits: int,
    ) -> None:
        self.uart_calls.append(("configure", {
            "baud_rate": baud_rate, "tx_idx": tx_idx, "rx_idx": rx_idx,
            "data_bits": data_bits, "parity": parity, "stop_bits": stop_bits,
        }))

    def uart_write(self, data: bytes) -> None:
        self.uart_calls.append(("write", {"data": data}))

    def uart_read(self, length: int, timeout_s: float) -> tuple[bytes, bool]:
        self.uart_calls.append(("read", {"length": length, "timeout_s": timeout_s}))
        return self._uart_canned_rx[:length], self._uart_parity_error

    # Test helpers
    def set_uart_canned_rx(self, data: bytes, parity_error: bool = False) -> None:
        self._uart_canned_rx = data
        self._uart_parity_error = parity_error

    # --- CAN (ProtocolCAN) ---

    def can_configure(self, tx_idx: int, rx_idx: int, bit_rate: int) -> None:
        self.can_calls.append(("configure", {
            "tx_idx": tx_idx, "rx_idx": rx_idx, "bit_rate": bit_rate,
        }))

    def can_send(self, id: int, data: bytes, extended: bool) -> None:
        self.can_calls.append(("send", {"id": id, "data": data, "extended": extended}))

    def can_receive(self, timeout_s: float) -> tuple[int | None, bytes, bool, int]:
        self.can_calls.append(("receive", {"timeout_s": timeout_s}))
        return self._can_canned_frame

    # Test helper
    def set_can_canned_frame(
        self, id: int | None, data: bytes, extended: bool, error_count: int
    ) -> None:
        self._can_canned_frame = (id, data, extended, error_count)
```

- [ ] **Step 4: Verify existing tests still pass**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/ -x -q
```

Expected: all existing tests pass (148 total).

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/fake.py
git commit -m "feat: backend stubs + FakeBackend for DMM, SPI, UART, CAN (stage 3b)"
```

---

## Task 2: DMM instrument + unit tests

**Files:**
- Create: `src/dwf_mcp/instruments/dmm.py`
- Create: `tests/unit/test_dmm.py`

### Background

DMM reuses `AnalogIn` hardware (same as scope). It is stateless — no configure step. Each `dmm.measure()` claims both `scope1` and `scope2` transiently (to get exclusive AnalogIn access, since AnalogIn has a shared acquisition engine), arms, polls for Done, reads, and releases in a `try/finally`. `dmm_stop()` is called in `finally` even on timeout to disarm AnalogIn before releasing the claim.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_dmm.py`:

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator, PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.dmm import DMM
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
def dmm(device: DwfDevice, tmp_path: Path) -> DMM:
    device.open()
    return DMM(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_measure_calls_backend_sequence(dmm: DMM) -> None:
    result = dmm.measure(channel=1, range_v=5.0)
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    names = [c[0] for c in fake.dmm_calls]
    assert names == ["configure", "arm", "stop"]


def test_measure_returns_statistics(dmm: DMM) -> None:
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    fake.set_dmm_canned_data(1, np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float64))
    result = dmm.measure(channel=1, range_v=5.0, n_averages=4)
    assert result["mean_v"] == pytest.approx(2.5)
    assert result["min_v"] == pytest.approx(1.0)
    assert result["max_v"] == pytest.approx(4.0)
    assert result["channel"] == 1
    assert result["range_v"] == 5.0
    assert result["coupling"] == "DC"


def test_measure_claim_released_after_call(dmm: DMM) -> None:
    dmm.measure(channel=1, range_v=5.0)
    assert dmm.device.allocator.claimed_pins() == {}


def test_measure_claims_both_scope_pins(dmm: DMM) -> None:
    # Intercept after configure to verify claim is held during measurement.
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    claimed: dict = {}

    original_arm = fake.dmm_arm
    def spy_arm() -> None:
        claimed.update(dmm.device.allocator.claimed_pins())
        original_arm()
    fake.dmm_arm = spy_arm  # type: ignore[method-assign]

    dmm.measure(channel=1, range_v=5.0)
    assert "scope1" in claimed
    assert "scope2" in claimed


def test_measure_raises_if_scope_holds_pin(dmm: DMM) -> None:
    dmm.device.allocator.claim("scope", ["scope1"])
    with pytest.raises(PinAllocationError):
        dmm.measure(channel=1, range_v=5.0)


def test_measure_releases_claim_on_backend_exception(dmm: DMM) -> None:
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    def raise_on_arm() -> None:
        raise RuntimeError("backend failed")
    fake.dmm_arm = raise_on_arm  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="backend failed"):
        dmm.measure(channel=1, range_v=5.0)
    assert dmm.device.allocator.claimed_pins() == {}


def test_measure_calls_dmm_stop_on_exception(dmm: DMM) -> None:
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    def raise_on_arm() -> None:
        raise RuntimeError("backend failed")
    fake.dmm_arm = raise_on_arm  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        dmm.measure(channel=1, range_v=5.0)
    stop_calls = [c for c in fake.dmm_calls if c[0] == "stop"]
    assert len(stop_calls) == 1


def test_measure_invalid_coupling_raises(dmm: DMM) -> None:
    with pytest.raises(ValueError, match="coupling"):
        dmm.measure(channel=1, range_v=5.0, coupling="INVALID")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_dmm.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'dwf_mcp.instruments.dmm'`

- [ ] **Step 3: Create `src/dwf_mcp/instruments/dmm.py`**

```python
"""DMM (voltmeter) instrument. Reuses AnalogIn with exclusive claim.

DMM and Scope both use AnalogIn, which has a single shared acquisition engine
(sample rate, mode, trigger are global). DMM therefore claims both scope1 and
scope2 to prevent concurrent Scope use, not just the channel being measured.
"""
from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

_VALID_COUPLINGS = {"DC", "AC"}

DMM_MEASURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "range_v"],
    "properties": {
        "channel": {"type": "integer", "enum": [1, 2]},
        "range_v": {"type": "number", "minimum": 0.001, "maximum": 50.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "n_averages": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 64},
    },
}


class DMM(Instrument):
    name = "dmm"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "measure": ("measure", DMM_MEASURE_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts

    def measure(
        self,
        channel: int,
        range_v: float,
        coupling: str = "DC",
        n_averages: int = 64,
    ) -> dict[str, Any]:
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(
                f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}"
            )
        self.device.allocator.claim("dmm", ["scope1", "scope2"])
        try:
            self.device.backend.dmm_configure(channel, range_v, coupling, n_averages)
            self.device.backend.dmm_arm()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if self.device.backend.dmm_status() == "Done":
                    break
            else:
                raise RuntimeError("DMM acquisition timed out after 2s")
            samples = self.device.backend.dmm_read(channel, n_averages)
        finally:
            try:
                self.device.backend.dmm_stop()
            except Exception:
                pass
            self.device.allocator.release("dmm")
        arr = np.asarray(samples, dtype=np.float64)
        return {
            "channel": channel,
            "mean_v": float(arr.mean()),
            "min_v": float(arr.min()),
            "max_v": float(arr.max()),
            "rms_v": float(np.sqrt(np.mean(arr**2))),
            "range_v": range_v,
            "coupling": coupling,
        }

    def release(self) -> None:
        self.device.allocator.release("dmm")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_dmm.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/dmm.py tests/unit/test_dmm.py
git commit -m "feat: DMM instrument (measure) + unit tests (stage 3b)"
```

---

## Task 3: SPI instrument + unit tests

**Files:**
- Create: `src/dwf_mcp/instruments/spi.py`
- Create: `tests/unit/test_spi.py`

### Background

SPI claims CLK + any of MOSI/MISO/CS provided at configure time. Partial-failure rollback: clear `_configured = False` before backend calls; on exception release claim and re-raise. Per-operation validation: each I/O method checks that the required pin was configured, raising `InstrumentNotConfigured` if not. `assert_cs=False` skips CS toggling within a transfer (for chained transactions) and does NOT require cs_pin; but `assert_cs=True` without cs_pin raises.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_spi.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator, PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.spi import SPI
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
def spi(device: DwfDevice, tmp_path: Path) -> SPI:
    device.open()
    return SPI(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pins(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3")
    claimed = spi.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"dio0", "dio1", "dio2", "dio3"}
    assert all(v == "spi" for v in claimed.values())


def test_configure_clk_only(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0)
    claimed = spi.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"dio0"}


def test_configure_calls_backend(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=500_000, mode=1,
                  mosi_pin="dio1", cs_pin="dio3")
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    cfg = fake.spi_calls[0]
    assert cfg[0] == "configure"
    assert cfg[1]["freq_hz"] == 500_000
    assert cfg[1]["mode"] == 1
    assert cfg[1]["mosi_idx"] == 1
    assert cfg[1]["miso_idx"] is None
    assert cfg[1]["cs_idx"] == 3


def test_configure_releases_on_backend_exception(spi: SPI) -> None:
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.spi_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    assert spi.device.allocator.claimed_pins() == {}
    assert not spi._configured


def test_reconfigure_failed_leaves_unconfigured(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.spi_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        spi.configure(clk_pin="dio0", frequency_hz=2_000_000, mode=0, mosi_pin="dio1")
    assert not spi._configured
    assert spi.device.allocator.claimed_pins() == {}


def test_transfer_full_duplex(spi: SPI) -> None:
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    fake.set_spi_canned_rx(bytes([0xAA, 0xBB]))
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3")
    result = spi.transfer(data=[0x01, 0x02])
    assert result["sent"] == [0x01, 0x02]
    assert result["received"] == [0xAA, 0xBB]


def test_transfer_requires_mosi(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, miso_pin="dio2")
    with pytest.raises(InstrumentNotConfigured, match="mosi_pin"):
        spi.transfer(data=[0x01])


def test_transfer_requires_miso(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    with pytest.raises(InstrumentNotConfigured, match="miso_pin"):
        spi.transfer(data=[0x01])


def test_assert_cs_true_without_cs_pin_raises(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2")
    with pytest.raises(InstrumentNotConfigured, match="cs_pin"):
        spi.transfer(data=[0x01], assert_cs=True)


def test_assert_cs_false_without_cs_pin_ok(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2")
    result = spi.transfer(data=[0x01], assert_cs=False)
    assert "sent" in result


def test_write_requires_mosi(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, miso_pin="dio2")
    with pytest.raises(InstrumentNotConfigured, match="mosi_pin"):
        spi.write(data=[0x01])


def test_read_requires_miso(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    with pytest.raises(InstrumentNotConfigured, match="miso_pin"):
        spi.read(length=2)


def test_unconfigured_raises(spi: SPI) -> None:
    with pytest.raises(InstrumentNotConfigured):
        spi.write(data=[0x01])


def test_release_clears_state(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    spi.release()
    assert not spi._configured
    assert spi.device.allocator.claimed_pins() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_spi.py -v 2>&1 | head -5
```

Expected: `ModuleNotFoundError: No module named 'dwf_mcp.instruments.spi'`

- [ ] **Step 3: Create `src/dwf_mcp/instruments/spi.py`**

```python
"""SPI active-master instrument. Wraps pydwf.ProtocolSPI via the DwfBackend seam.

assert_cs=False means "do not toggle CS within this transfer" (for chaining
back-to-back transfers while holding CS low). To use an external DIO for CS,
omit cs_pin from configure entirely.
"""
from __future__ import annotations

import re
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_DIO_PATTERN = re.compile(r"^dio(\d+)$")


def _dio_index(pin: str) -> int:
    m = _DIO_PATTERN.match(pin)
    if not m:
        raise ValueError(f"expected pin like 'dio0'..'dio15', got {pin!r}")
    return int(m.group(1))


SPI_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["clk_pin", "frequency_hz", "mode"],
    "properties": {
        "clk_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "frequency_hz": {"type": "number", "minimum": 1, "maximum": 50_000_000},
        "mode": {"type": "integer", "enum": [0, 1, 2, 3]},
        "mosi_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "miso_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "cs_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "cs_polarity": {
            "type": "string", "enum": ["active_low", "active_high"],
            "default": "active_low",
        },
        "bit_order": {"type": "string", "enum": ["msb", "lsb"], "default": "msb"},
    },
}

SPI_TRANSFER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["data"],
    "properties": {
        "data": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
        },
        "assert_cs": {"type": "boolean", "default": True},
    },
}

SPI_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["data"],
    "properties": {
        "data": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
        },
        "assert_cs": {"type": "boolean", "default": True},
    },
}

SPI_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["length"],
    "properties": {
        "length": {"type": "integer", "minimum": 1, "maximum": 65536},
        "assert_cs": {"type": "boolean", "default": True},
    },
}


class SPI(Instrument):
    name = "spi"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", SPI_CONFIGURE_SCHEMA),
        "transfer":  ("transfer",  SPI_TRANSFER_SCHEMA),
        "write":     ("write",     SPI_WRITE_SCHEMA),
        "read":      ("read",      SPI_READ_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False
        self._mosi_pin: str | None = None
        self._miso_pin: str | None = None
        self._cs_pin: str | None = None

    def configure(
        self,
        clk_pin: str,
        frequency_hz: float,
        mode: int,
        mosi_pin: str | None = None,
        miso_pin: str | None = None,
        cs_pin: str | None = None,
        cs_polarity: str = "active_low",
        bit_order: str = "msb",
    ) -> dict[str, Any]:
        pins = [p for p in [clk_pin, mosi_pin, miso_pin, cs_pin] if p is not None]
        clk_idx = _dio_index(clk_pin)
        mosi_idx = _dio_index(mosi_pin) if mosi_pin else None
        miso_idx = _dio_index(miso_pin) if miso_pin else None
        cs_idx = _dio_index(cs_pin) if cs_pin else None

        self.device.allocator.claim("spi", pins)
        self._configured = False
        self._mosi_pin = None
        self._miso_pin = None
        self._cs_pin = None
        try:
            self.device.backend.spi_configure(
                clk_idx=clk_idx, freq_hz=frequency_hz, mode=mode,
                mosi_idx=mosi_idx, miso_idx=miso_idx, cs_idx=cs_idx,
                cs_polarity=cs_polarity, bit_order=bit_order,
            )
        except Exception:
            self.device.allocator.release("spi")
            raise
        self._configured = True
        self._mosi_pin = mosi_pin
        self._miso_pin = miso_pin
        self._cs_pin = cs_pin
        return {
            "configured": True,
            "clk_pin": clk_pin,
            "frequency_hz": frequency_hz,
            "mode": mode,
        }

    def transfer(self, data: list[int], assert_cs: bool = True) -> dict[str, Any]:
        self._require_configured()
        if self._mosi_pin is None:
            raise InstrumentNotConfigured("spi.transfer requires mosi_pin to be configured")
        if self._miso_pin is None:
            raise InstrumentNotConfigured("spi.transfer requires miso_pin to be configured")
        if assert_cs and self._cs_pin is None:
            raise InstrumentNotConfigured(
                "spi.transfer with assert_cs=True requires cs_pin to be configured"
            )
        received = self.device.backend.spi_transfer(bytes(data), assert_cs)
        return {"sent": list(data), "received": list(received)}

    def write(self, data: list[int], assert_cs: bool = True) -> dict[str, Any]:
        self._require_configured()
        if self._mosi_pin is None:
            raise InstrumentNotConfigured("spi.write requires mosi_pin to be configured")
        if assert_cs and self._cs_pin is None:
            raise InstrumentNotConfigured(
                "spi.write with assert_cs=True requires cs_pin to be configured"
            )
        self.device.backend.spi_write(bytes(data), assert_cs)
        return {"bytes_written": len(data)}

    def read(self, length: int, assert_cs: bool = True) -> dict[str, Any]:
        self._require_configured()
        if self._miso_pin is None:
            raise InstrumentNotConfigured("spi.read requires miso_pin to be configured")
        if assert_cs and self._cs_pin is None:
            raise InstrumentNotConfigured(
                "spi.read with assert_cs=True requires cs_pin to be configured"
            )
        data = self.device.backend.spi_read(length, assert_cs)
        return {"data": list(data), "data_hex": data.hex()}

    def release(self) -> None:
        self.device.allocator.release("spi")
        self._configured = False
        self._mosi_pin = None
        self._miso_pin = None
        self._cs_pin = None

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("spi.configure must be called before any I/O operation")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_spi.py -v
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/spi.py tests/unit/test_spi.py
git commit -m "feat: SPI instrument (configure/transfer/write/read) + unit tests (stage 3b)"
```

---

## Task 4: UART instrument + unit tests

**Files:**
- Create: `src/dwf_mcp/instruments/uart.py`
- Create: `tests/unit/test_uart.py`

### Background

UART claims TX and/or RX pins (at least one required). `uart.read()` returns whatever bytes arrived before timeout — may be fewer than `length`. `uart.write()` requires TX, `uart.read()` requires RX; each raises `InstrumentNotConfigured` if the pin wasn't configured.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_uart.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.uart import UART
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
def uart(device: DwfDevice, tmp_path: Path) -> UART:
    device.open()
    return UART(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_both_pins_claims_both(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0", rx_pin="dio1")
    claimed = uart.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"dio0", "dio1"}


def test_configure_tx_only(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0")
    assert set(uart.device.allocator.claimed_pins().keys()) == {"dio0"}


def test_configure_rx_only(uart: UART) -> None:
    uart.configure(baud_rate=115200, rx_pin="dio1")
    assert set(uart.device.allocator.claimed_pins().keys()) == {"dio1"}


def test_configure_neither_raises(uart: UART) -> None:
    with pytest.raises(ValueError, match="tx_pin or rx_pin"):
        uart.configure(baud_rate=115200)


def test_configure_calls_backend(uart: UART) -> None:
    uart.configure(baud_rate=9600, tx_pin="dio0", rx_pin="dio1",
                   data_bits=7, parity="odd", stop_bits=2)
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    cfg = fake.uart_calls[0]
    assert cfg[0] == "configure"
    assert cfg[1]["baud_rate"] == 9600
    assert cfg[1]["parity"] == "odd"
    assert cfg[1]["data_bits"] == 7
    assert cfg[1]["stop_bits"] == 2


def test_configure_releases_on_exception(uart: UART) -> None:
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.uart_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        uart.configure(baud_rate=115200, tx_pin="dio0")
    assert uart.device.allocator.claimed_pins() == {}


def test_write_sends_data(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0")
    result = uart.write(data=[0x48, 0x65, 0x6C, 0x6C, 0x6F])
    assert result == {"bytes_written": 5}
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    writes = [c for c in fake.uart_calls if c[0] == "write"]
    assert writes[0][1]["data"] == b"Hello"


def test_write_without_tx_pin_raises(uart: UART) -> None:
    uart.configure(baud_rate=115200, rx_pin="dio1")
    with pytest.raises(InstrumentNotConfigured, match="tx_pin"):
        uart.write(data=[0x01])


def test_read_returns_data_and_parity_flag(uart: UART) -> None:
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    fake.set_uart_canned_rx(b"\xDE\xAD", parity_error=True)
    uart.configure(baud_rate=115200, rx_pin="dio1")
    result = uart.read(length=2)
    assert result["data"] == [0xDE, 0xAD]
    assert result["data_hex"] == "dead"
    assert result["parity_error"] is True


def test_read_partial_on_timeout(uart: UART) -> None:
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    fake.set_uart_canned_rx(b"\x01")  # only 1 byte even though 4 requested
    uart.configure(baud_rate=115200, rx_pin="dio1")
    result = uart.read(length=4)
    assert result["data"] == [0x01]  # partial result, not an error


def test_read_without_rx_pin_raises(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0")
    with pytest.raises(InstrumentNotConfigured, match="rx_pin"):
        uart.read(length=1)


def test_unconfigured_raises(uart: UART) -> None:
    with pytest.raises(InstrumentNotConfigured):
        uart.write(data=[0x01])


def test_release_clears_state(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0", rx_pin="dio1")
    uart.release()
    assert not uart._configured
    assert uart.device.allocator.claimed_pins() == {}
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_uart.py -v 2>&1 | head -5
```

Expected: `ModuleNotFoundError: No module named 'dwf_mcp.instruments.uart'`

- [ ] **Step 3: Create `src/dwf_mcp/instruments/uart.py`**

```python
"""UART instrument. Wraps pydwf.ProtocolUART via the DwfBackend seam."""
from __future__ import annotations

import re
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_DIO_PATTERN = re.compile(r"^dio(\d+)$")


def _dio_index(pin: str) -> int:
    m = _DIO_PATTERN.match(pin)
    if not m:
        raise ValueError(f"expected pin like 'dio0'..'dio15', got {pin!r}")
    return int(m.group(1))


UART_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["baud_rate"],
    "properties": {
        "baud_rate": {"type": "integer", "minimum": 300, "maximum": 4_000_000},
        "tx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "rx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {
            "type": "string", "enum": ["none", "odd", "even"], "default": "none",
        },
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
    },
}

UART_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["data"],
    "properties": {
        "data": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
        },
    },
}

UART_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["length"],
    "properties": {
        "length": {"type": "integer", "minimum": 1, "maximum": 65536},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}


class UART(Instrument):
    name = "uart"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", UART_CONFIGURE_SCHEMA),
        "write":     ("write",     UART_WRITE_SCHEMA),
        "read":      ("read",      UART_READ_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False
        self._tx_pin: str | None = None
        self._rx_pin: str | None = None

    def configure(
        self,
        baud_rate: int,
        tx_pin: str | None = None,
        rx_pin: str | None = None,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
    ) -> dict[str, Any]:
        if tx_pin is None and rx_pin is None:
            raise ValueError("at least one of tx_pin or rx_pin must be provided")
        pins = [p for p in [tx_pin, rx_pin] if p is not None]
        tx_idx = _dio_index(tx_pin) if tx_pin else None
        rx_idx = _dio_index(rx_pin) if rx_pin else None

        self.device.allocator.claim("uart", pins)
        self._configured = False
        self._tx_pin = None
        self._rx_pin = None
        try:
            self.device.backend.uart_configure(
                baud_rate=baud_rate, tx_idx=tx_idx, rx_idx=rx_idx,
                data_bits=data_bits, parity=parity, stop_bits=stop_bits,
            )
        except Exception:
            self.device.allocator.release("uart")
            raise
        self._configured = True
        self._tx_pin = tx_pin
        self._rx_pin = rx_pin
        return {"configured": True, "baud_rate": baud_rate}

    def write(self, data: list[int]) -> dict[str, Any]:
        self._require_configured()
        if self._tx_pin is None:
            raise InstrumentNotConfigured("uart.write requires tx_pin to be configured")
        self.device.backend.uart_write(bytes(data))
        return {"bytes_written": len(data)}

    def read(self, length: int, timeout_s: float = 1.0) -> dict[str, Any]:
        self._require_configured()
        if self._rx_pin is None:
            raise InstrumentNotConfigured("uart.read requires rx_pin to be configured")
        data, parity_error = self.device.backend.uart_read(length, timeout_s)
        return {"data": list(data), "data_hex": data.hex(), "parity_error": parity_error}

    def release(self) -> None:
        self.device.allocator.release("uart")
        self._configured = False
        self._tx_pin = None
        self._rx_pin = None

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("uart.configure must be called before any I/O operation")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_uart.py -v
```

Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/uart.py tests/unit/test_uart.py
git commit -m "feat: UART instrument (configure/write/read) + unit tests (stage 3b)"
```

---

## Task 5: CAN instrument + unit tests

**Files:**
- Create: `src/dwf_mcp/instruments/can.py`
- Create: `tests/unit/test_can.py`

### Background

CAN requires both TX and RX. `can.receive()` returns `id=None` on timeout (not an error). Standard CAN IDs are 11-bit (≤0x7FF); extended IDs are 29-bit.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_can.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.can import CAN
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
def can(device: DwfDevice, tmp_path: Path) -> CAN:
    device.open()
    return CAN(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_both_pins(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    claimed = can.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"dio0", "dio1"}


def test_configure_calls_backend(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=250_000)
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    cfg = fake.can_calls[0]
    assert cfg[0] == "configure"
    assert cfg[1]["tx_idx"] == 0
    assert cfg[1]["rx_idx"] == 1
    assert cfg[1]["bit_rate"] == 250_000


def test_configure_releases_on_exception(can: CAN) -> None:
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.can_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    assert can.device.allocator.claimed_pins() == {}


def test_send_standard_frame(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.send(id=0x123, data=[0x01, 0x02, 0x03])
    assert result == {"sent": True}
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    sends = [c for c in fake.can_calls if c[0] == "send"]
    assert sends[0][1]["id"] == 0x123
    assert sends[0][1]["data"] == b"\x01\x02\x03"
    assert sends[0][1]["extended"] is False


def test_send_extended_frame(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.send(id=0x12345678, data=[0xFF], extended=True)
    assert result == {"sent": True}
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    sends = [c for c in fake.can_calls if c[0] == "send"]
    assert sends[0][1]["extended"] is True


def test_send_standard_id_too_large_raises(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    with pytest.raises(ValueError, match="0x7FF"):
        can.send(id=0x800, data=[], extended=False)


def test_receive_frame(can: CAN) -> None:
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    fake.set_can_canned_frame(id=0x456, data=b"\xDE\xAD", extended=False, error_count=0)
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.receive()
    assert result["id"] == 0x456
    assert result["data"] == [0xDE, 0xAD]
    assert result["data_hex"] == "dead"
    assert result["extended"] is False
    assert result["error_count"] == 0


def test_receive_timeout_returns_none_id(can: CAN) -> None:
    # Default canned frame has id=None (timeout).
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.receive(timeout_s=0.1)
    assert result["id"] is None
    assert result["data"] == []
    assert result["error_count"] == 0


def test_receive_propagates_error_count(can: CAN) -> None:
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    fake.set_can_canned_frame(id=0x1, data=b"\x00", extended=False, error_count=5)
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.receive()
    assert result["error_count"] == 5


def test_unconfigured_raises(can: CAN) -> None:
    with pytest.raises(InstrumentNotConfigured):
        can.send(id=0x1, data=[])


def test_release_clears_state(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    can.release()
    assert not can._configured
    assert can.device.allocator.claimed_pins() == {}
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_can.py -v 2>&1 | head -5
```

Expected: `ModuleNotFoundError: No module named 'dwf_mcp.instruments.can'`

- [ ] **Step 3: Create `src/dwf_mcp/instruments/can.py`**

```python
"""CAN active-master instrument. Wraps pydwf.ProtocolCAN via the DwfBackend seam."""
from __future__ import annotations

import re
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_DIO_PATTERN = re.compile(r"^dio(\d+)$")


def _dio_index(pin: str) -> int:
    m = _DIO_PATTERN.match(pin)
    if not m:
        raise ValueError(f"expected pin like 'dio0'..'dio15', got {pin!r}")
    return int(m.group(1))


CAN_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tx_pin", "rx_pin", "bit_rate"],
    "properties": {
        "tx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "rx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "bit_rate": {"type": "integer", "minimum": 1000, "maximum": 1_000_000},
    },
}

CAN_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "data"],
    "properties": {
        "id": {"type": "integer", "minimum": 0, "maximum": 0x1FFFFFFF},
        "data": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
            "maxItems": 8,
        },
        "extended": {"type": "boolean", "default": False},
    },
}

CAN_RECEIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}


class CAN(Instrument):
    name = "can"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", CAN_CONFIGURE_SCHEMA),
        "send":      ("send",      CAN_SEND_SCHEMA),
        "receive":   ("receive",   CAN_RECEIVE_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False

    def configure(
        self,
        tx_pin: str,
        rx_pin: str,
        bit_rate: int,
    ) -> dict[str, Any]:
        tx_idx = _dio_index(tx_pin)
        rx_idx = _dio_index(rx_pin)
        self.device.allocator.claim("can", [tx_pin, rx_pin])
        self._configured = False
        try:
            self.device.backend.can_configure(
                tx_idx=tx_idx, rx_idx=rx_idx, bit_rate=bit_rate,
            )
        except Exception:
            self.device.allocator.release("can")
            raise
        self._configured = True
        return {"configured": True, "tx_pin": tx_pin, "rx_pin": rx_pin, "bit_rate": bit_rate}

    def send(self, id: int, data: list[int], extended: bool = False) -> dict[str, Any]:
        self._require_configured()
        if not extended and id > 0x7FF:
            raise ValueError(
                f"standard CAN ID must be ≤ 0x7FF, got {id:#x}; use extended=True for 29-bit IDs"
            )
        self.device.backend.can_send(id=id, data=bytes(data), extended=extended)
        return {"sent": True}

    def receive(self, timeout_s: float = 1.0) -> dict[str, Any]:
        self._require_configured()
        frame_id, data, extended, error_count = self.device.backend.can_receive(timeout_s)
        if frame_id is None:
            return {"id": None, "data": [], "data_hex": "", "extended": False,
                    "error_count": error_count}
        return {
            "id": frame_id,
            "data": list(data),
            "data_hex": data.hex(),
            "extended": extended,
            "error_count": error_count,
        }

    def release(self) -> None:
        self.device.allocator.release("can")
        self._configured = False

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("can.configure must be called before any I/O operation")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/test_can.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/can.py tests/unit/test_can.py
git commit -m "feat: CAN instrument (configure/send/receive) + unit tests (stage 3b)"
```

---

## Task 6: PydwfBackend implementations

**Files:**
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`

### Background

All four instruments use `self._device.protocol.<name>` (same pattern as `self._device.protocol.i2c`). Verify the exact pydwf API names before implementing by running: `python -c "from pydwf import DwfLibrary; d = DwfLibrary().deviceControl.open(0); help(d.protocol.spi)"` with hardware connected. The method names below are based on pydwf 1.1.x conventions — confirm against actual pydwf source if any call fails.

- [ ] **Step 1: Add DMM backend implementation**

After the `# Logic record-mode` section, append:

```python
    # --- DMM (AnalogIn measurement) -------------------------------------------

    def dmm_configure(
        self, channel: int, range_v: float, coupling: str, n_averages: int
    ) -> None:
        ain = self._analog_in
        ch_idx = channel - 1
        # Disable both channels first, then enable only the measured channel.
        ain.channelEnableSet(0, False)
        ain.channelEnableSet(1, False)
        ain.channelEnableSet(ch_idx, True)
        ain.channelRangeSet(ch_idx, range_v)
        ain.channelOffsetSet(ch_idx, 0.0)
        cp = DwfAnalogCoupling.DC if coupling == "DC" else DwfAnalogCoupling.AC
        ain.channelCouplingSet(ch_idx, cp)
        ain.frequencySet(1000.0)  # 1 kHz adequate for voltage measurement
        ain.bufferSizeSet(n_averages)
        ain.acquisitionModeSet(DwfAcquisitionMode.Single)

    def dmm_arm(self) -> None:
        self._analog_in.configure(False, True)  # reconfigure=False, start=True

    def dmm_status(self) -> str:
        st = self._analog_in.status(True)
        return "Done" if st == DwfState.Done else str(getattr(st, "name", st))

    def dmm_read(self, channel: int, count: int) -> np.ndarray:
        return np.asarray(
            self._analog_in.statusData(channel - 1, count), dtype=np.float64
        )

    def dmm_stop(self) -> None:
        try:
            self._analog_in.configure(False, False)  # apply params without starting
        except Exception:
            pass
```

- [ ] **Step 2: Add SPI backend implementation**

```python
    # --- SPI (ProtocolSPI) ----------------------------------------------------

    @property
    def _spi(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.spi

    def spi_configure(
        self, clk_idx: int, freq_hz: float, mode: int,
        mosi_idx: int | None, miso_idx: int | None, cs_idx: int | None,
        cs_polarity: str, bit_order: str,
    ) -> None:
        spi = self._spi
        spi.reset()
        spi.frequencySet(freq_hz)
        spi.modeSet(mode)
        spi.orderMsbSet(bit_order == "msb")
        spi.clockSet(clk_idx)
        if mosi_idx is not None:
            spi.dataSet(mosi_idx, 0)   # 0 = DQ0 (MOSI)
        if miso_idx is not None:
            spi.dataSet(miso_idx, 1)   # 1 = DQ1 (MISO)
        if cs_idx is not None:
            polarity = 0 if cs_polarity == "active_low" else 1
            spi.selectSet(cs_idx, polarity)

    def spi_transfer(self, data: bytes, assert_cs: bool) -> bytes:
        # dcs=1: assert CS before transfer, deassert after.
        # dcs=0: no CS change (assert_cs=False for chained transfers).
        dcs = 1 if assert_cs else 0
        rx = self._spi.writeRead(dcs, len(data) * 8, list(data))
        return bytes(rx)

    def spi_write(self, data: bytes, assert_cs: bool) -> None:
        dcs = 1 if assert_cs else 0
        self._spi.write(dcs, len(data) * 8, list(data))

    def spi_read(self, length: int, assert_cs: bool) -> bytes:
        dcs = 1 if assert_cs else 0
        rx = self._spi.read(dcs, length * 8)
        return bytes(rx)
```

- [ ] **Step 3: Add UART backend implementation**

```python
    # --- UART (ProtocolUART) --------------------------------------------------

    @property
    def _uart(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.uart

    def uart_configure(
        self, baud_rate: int, tx_idx: int | None, rx_idx: int | None,
        data_bits: int, parity: str, stop_bits: int,
    ) -> None:
        uart = self._uart
        uart.reset()
        uart.baudrateSet(baud_rate)
        uart.dataBitsSet(data_bits)
        parity_map = {"none": 0, "odd": 1, "even": 2}
        uart.paritySet(parity_map[parity])
        uart.stopBitsSet(stop_bits)
        if tx_idx is not None:
            uart.txSet(tx_idx)
        if rx_idx is not None:
            uart.rxSet(rx_idx)
        uart.enable()

    def uart_write(self, data: bytes) -> None:
        self._uart.tx(list(data))

    def uart_read(self, length: int, timeout_s: float) -> tuple[bytes, bool]:
        # pydwf uart.rx returns (parity_error: int, data: list[int])
        parity_err, rx_data = self._uart.rx(length)
        return bytes(rx_data), bool(parity_err)
```

- [ ] **Step 4: Add CAN backend implementation**

```python
    # --- CAN (ProtocolCAN) ----------------------------------------------------

    @property
    def _can(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.can

    def can_configure(self, tx_idx: int, rx_idx: int, bit_rate: int) -> None:
        can = self._can
        can.reset()
        can.rateSet(bit_rate)
        can.txSet(tx_idx)
        can.rxSet(rx_idx)

    def can_send(self, id: int, data: bytes, extended: bool) -> None:
        self._can.tx(id, int(extended), list(data))

    def can_receive(self, timeout_s: float) -> tuple[int | None, bytes, bool, int]:
        import time
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # pydwf can.rx() returns (status, id, ext, data, count_error)
            # status 0 = no frame available, non-zero = frame received
            status, frame_id, ext, data, error_count = self._can.rx()
            if status:
                return frame_id, bytes(data), bool(ext), error_count
            time.sleep(0.001)
        # Timeout: return id=None sentinel.
        return None, b"", False, 0
```

- [ ] **Step 5: Run existing unit tests to confirm nothing broken**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/ -x -q
```

Expected: all tests pass (148 + 8 + 15 + 13 + 11 = ~195 total).

- [ ] **Step 6: Commit**

```bash
git add src/dwf_mcp/backends/pydwf_backend.py
git commit -m "feat: PydwfBackend for DMM, SPI, UART, CAN (stage 3b)"
```

---

## Task 7: Register instruments in `server.py` + run full test suite

**Files:**
- Modify: `src/dwf_mcp/server.py`

- [ ] **Step 1: Add imports and register in `build_app`**

At the top of `server.py`, add four imports after the existing instrument imports:

```python
from dwf_mcp.instruments.can import CAN
from dwf_mcp.instruments.dmm import DMM
from dwf_mcp.instruments.spi import SPI
from dwf_mcp.instruments.uart import UART
```

In `build_app()`, after `app.register_instrument(Logic)`, add:

```python
    app.register_instrument(DMM)
    app.register_instrument(SPI)
    app.register_instrument(UART)
    app.register_instrument(CAN)
```

- [ ] **Step 2: Run full test suite**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/ tests/integration/ -q
```

Expected: all tests pass. Count should be ~195+ with no failures.

- [ ] **Step 3: Verify tool count via server status**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && python -c "
from dwf_mcp.server import build_app
app = build_app(backend_name='fake')
print('Tool count:', len(app._tools))
print('New tools:', [t for t in app._tools if t.startswith(('dmm.','spi.','uart.','can.'))])
"
```

Expected: Tool count 43, new tools `['dmm.measure', 'spi.configure', 'spi.transfer', 'spi.write', 'spi.read', 'uart.configure', 'uart.write', 'uart.read', 'can.configure', 'can.send', 'can.receive']` (plus 3 waveforms meta + 29 existing = 43 but meta are already counted).

- [ ] **Step 4: Commit**

```bash
git add src/dwf_mcp/server.py
git commit -m "feat: register DMM, SPI, UART, CAN in build_app (stage 3b instruments complete)"
```

---

## Task 8: Hardware smoke tests

**Files:**
- Create: `tests/hardware/test_dmm_hardware.py`
- Create: `tests/hardware/test_spi_hardware.py`
- Create: `tests/hardware/test_uart_hardware.py`
- Create: `tests/hardware/test_can_hardware.py`

### Wiring required before running hardware tests

- **DMM:** W1 (AWG channel 1) → Scope 1+ (scope channel 1 positive). Configure AWG to output 2.0V DC, then measure with DMM channel 1.
- **SPI:** DIO0 (CLK) → self, DIO1 (MOSI) → DIO2 (MISO) loopback wire, DIO3 (CS).
- **UART:** DIO0 (TX) → DIO1 (RX) loopback wire.
- **CAN:** DIO0 (TX) → DIO1 (RX) direct wire at low bit rate (≤125kbps; no termination needed for short loopback).

- [ ] **Step 1: Create `tests/hardware/test_dmm_hardware.py`**

```python
"""DMM hardware smoke test. Requires W1→Scope1+ loopback and AD3 connected."""
from __future__ import annotations

import pytest

from dwf_mcp.server import build_app


@pytest.mark.hardware
def test_dmm_measures_awg_dc_voltage() -> None:
    app = build_app(backend_name="pydwf")
    app.call_tool.__func__  # just verify it's accessible
    import asyncio

    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        # Set W1 to 2.0V DC.
        await app.call_tool("awg.configure", {
            "channel": 1, "function": "DC",
            "frequency_hz": 1000.0, "amplitude_v": 2.0,
            "offset_v": 0.0, "phase_deg": 0.0, "symmetry": 50.0,
        })
        await app.call_tool("awg.start", {"channel": 1})
        import time; time.sleep(0.05)
        result = await app.call_tool("dmm.measure", {"channel": 1, "range_v": 5.0})
        assert "mean_v" in result
        assert abs(result["mean_v"] - 2.0) < 0.1, f"expected ~2.0V, got {result['mean_v']}"
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("waveforms.close", {})

    asyncio.run(run())
```

- [ ] **Step 2: Create `tests/hardware/test_spi_hardware.py`**

```python
"""SPI hardware smoke test. Requires MOSI(DIO1)→MISO(DIO2) loopback."""
from __future__ import annotations

import asyncio
import pytest

from dwf_mcp.server import build_app


@pytest.mark.hardware
def test_spi_loopback_transfer() -> None:
    app = build_app(backend_name="pydwf")

    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "frequency_hz": 1_000_000, "mode": 0,
            "mosi_pin": "dio1", "miso_pin": "dio2", "cs_pin": "dio3",
        })
        result = await app.call_tool("spi.transfer", {"data": [0xA5, 0x5A, 0xFF, 0x00]})
        assert result["sent"] == [0xA5, 0x5A, 0xFF, 0x00]
        # Loopback: received should equal sent.
        assert result["received"] == result["sent"]
        await app.call_tool("waveforms.close", {})

    asyncio.run(run())
```

- [ ] **Step 3: Create `tests/hardware/test_uart_hardware.py`**

```python
"""UART hardware smoke test. Requires TX(DIO0)→RX(DIO1) loopback."""
from __future__ import annotations

import asyncio
import pytest

from dwf_mcp.server import build_app


@pytest.mark.hardware
def test_uart_loopback() -> None:
    app = build_app(backend_name="pydwf")

    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        await app.call_tool("uart.configure", {
            "baud_rate": 9600, "tx_pin": "dio0", "rx_pin": "dio1",
        })
        await app.call_tool("uart.write", {"data": [0x48, 0x65, 0x6C, 0x6C, 0x6F]})
        import time; time.sleep(0.05)  # wait for bytes to travel
        result = await app.call_tool("uart.read", {"length": 5, "timeout_s": 1.0})
        assert result["data"] == [0x48, 0x65, 0x6C, 0x6C, 0x6F], f"got: {result['data']}"
        assert result["parity_error"] is False
        await app.call_tool("waveforms.close", {})

    asyncio.run(run())
```

- [ ] **Step 4: Create `tests/hardware/test_can_hardware.py`**

```python
"""CAN hardware smoke test. Requires TX(DIO0)→RX(DIO1) loopback at 125kbps."""
from __future__ import annotations

import asyncio
import pytest

from dwf_mcp.server import build_app


@pytest.mark.hardware
def test_can_send_receive_loopback() -> None:
    app = build_app(backend_name="pydwf")

    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        await app.call_tool("can.configure", {
            "tx_pin": "dio0", "rx_pin": "dio1", "bit_rate": 125_000,
        })
        await app.call_tool("can.send", {"id": 0x123, "data": [0x01, 0x02, 0x03]})
        result = await app.call_tool("can.receive", {"timeout_s": 1.0})
        assert result["id"] == 0x123, f"expected 0x123, got {result['id']}"
        assert result["data"] == [0x01, 0x02, 0x03]
        assert result["extended"] is False
        await app.call_tool("waveforms.close", {})

    asyncio.run(run())
```

- [ ] **Step 5: Verify hardware tests are skipped in normal run**

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/ -q --ignore=tests/hardware
```

Expected: all non-hardware tests pass, hardware tests not collected.

- [ ] **Step 6: Commit**

```bash
git add tests/hardware/test_dmm_hardware.py tests/hardware/test_spi_hardware.py \
        tests/hardware/test_uart_hardware.py tests/hardware/test_can_hardware.py
git commit -m "test: hardware smoke tests for DMM, SPI, UART, CAN (stage 3b)"
```

---

**Instruments workstream complete.** Run the full suite one final time to confirm baseline:

```bash
cd /Users/claude/work/dwf-mcp/dwf-mcp && .venv/bin/pytest tests/unit/ tests/integration/ -q
```

Expected: ~195+ passed, 0 failed, hardware tests deselected.
