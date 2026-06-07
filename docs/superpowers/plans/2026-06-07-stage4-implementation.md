# Stage 4 Implementation Plan: Protocol Sniffing and Decoding

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `sniff` and `decoder` instruments for passive protocol capture (I2C/SPI/UART/CAN) with parquet artifact output and a sigrokdecode-compatible `Decoder` ABC.

**Architecture:** Hardware protocol engines handle I2C/UART/CAN sniff; SPI sniff uses DigitalIn record mode (start/status/stop pattern). A `SpiDecoder` state machine decodes raw samples. The allocator gains engine-resource virtual pins and a `claim_observe` method for DigitalIn/DigitalOut coexistence.

**Tech Stack:** Python 3.12, pyarrow (already a dep), numpy, pydwf, asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-06-07-stage4-design.md`

---

## File map

**Create:**
- `src/dwf_mcp/instruments/sniff.py` — Sniff instrument (6 tools: i2c, uart, can, spi_start, spi_status, spi_stop)
- `src/dwf_mcp/instruments/decoder/__init__.py` — Decoder instrument (decoder.spi tool)
- `src/dwf_mcp/instruments/decoder/base.py` — Decoder ABC + SpiTransaction dataclass
- `src/dwf_mcp/instruments/decoder/spi.py` — SpiDecoder state machine
- `tests/unit/test_spi_decoder.py` — SpiDecoder golden tests
- `tests/unit/test_sniff.py` — Sniff instrument unit tests (FakeBackend)
- `tests/hardware/test_sniff_spi_hardware.py` — SPI sniff + decode hardware test
- `tests/hardware/test_sniff_uart_hardware.py` — UART sniff stub
- `tests/hardware/test_sniff_i2c_hardware.py` — I2C sniff stub
- `tests/hardware/test_sniff_can_hardware.py` — CAN sniff stub

**Modify:**
- `src/dwf_mcp/allocator.py` — add `_observe_claims`, `claim_observe`, update `claim`/`release`/`clear`
- `src/dwf_mcp/devices/ad3.py` — add engine resource virtual pin constants
- `src/dwf_mcp/artifacts.py` — add `write_parquet`
- `src/dwf_mcp/backend.py` — add ABC stubs for i2c_spy_*, uart_sniff, can_sniff
- `src/dwf_mcp/backends/fake.py` — stubs for above
- `src/dwf_mcp/backends/pydwf_backend.py` — real implementations
- `src/dwf_mcp/instruments/i2c.py` — claim `i2c_engine` virtual pin
- `src/dwf_mcp/instruments/spi.py` — claim `spi_engine` virtual pin
- `src/dwf_mcp/instruments/uart.py` — claim `uart_engine` virtual pin
- `src/dwf_mcp/instruments/can.py` — claim `can_engine` virtual pin
- `src/dwf_mcp/instruments/logic.py` — claim `digital_in` virtual pin
- `src/dwf_mcp/server.py` — import + register Sniff and Decoder
- `tests/unit/test_artifacts.py` — extend to cover write_parquet

---

## Task 1: Engine resource virtual pins

Engine resources prevent two instruments from silently sharing the same hardware protocol engine. We add virtual pin names (e.g. `"i2c_engine"`) that each instrument includes in its allocator claim. Since `claim()` uses replacement semantics per instrument name, re-configuring the same instrument is still allowed.

**Files:**
- Modify: `src/dwf_mcp/devices/ad3.py`
- Modify: `src/dwf_mcp/instruments/i2c.py:95`
- Modify: `src/dwf_mcp/instruments/spi.py:87`
- Modify: `src/dwf_mcp/instruments/uart.py:76`
- Modify: `src/dwf_mcp/instruments/can.py:63`

- [ ] **Step 1.1: Add engine pin constants to ad3.py**

```python
# Add after AD3_TRIGGER_PINS line in src/dwf_mcp/devices/ad3.py:

# Virtual resource names for hardware protocol engines (not physical pins).
# Including these in allocator.claim() prevents two different instruments from
# silently reconfiguring the same hardware engine.
AD3_ENGINE_PINS = {
    "i2c": "i2c_engine",
    "spi": "spi_engine",
    "uart": "uart_engine",
    "can": "can_engine",
    "digital_in": "digital_in",
}
```

- [ ] **Step 1.2: Update i2c.configure to claim engine resource**

In `src/dwf_mcp/instruments/i2c.py`, change line 95:
```python
# Before:
self.device.allocator.claim("i2c", [sda_pin, scl_pin])
# After:
self.device.allocator.claim("i2c", ["i2c_engine", sda_pin, scl_pin])
```

- [ ] **Step 1.3: Update spi.configure to claim engine resource**

In `src/dwf_mcp/instruments/spi.py`, find `self.device.allocator.claim("spi", pins)` and change to:
```python
self.device.allocator.claim("spi", ["spi_engine"] + pins)
```

- [ ] **Step 1.4: Update uart.configure to claim engine resource**

In `src/dwf_mcp/instruments/uart.py`, find `self.device.allocator.claim("uart", pins)` and change to:
```python
self.device.allocator.claim("uart", ["uart_engine"] + pins)
```

- [ ] **Step 1.5: Update can.configure to claim engine resource**

In `src/dwf_mcp/instruments/can.py`, find `self.device.allocator.claim("can", [tx_pin, rx_pin])` and change to:
```python
self.device.allocator.claim("can", ["can_engine", tx_pin, rx_pin])
```

- [ ] **Step 1.6: Run tests to confirm no regressions**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: `250 passed` (same count as before — engine pins don't break existing tests)

- [ ] **Step 1.7: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/devices/ad3.py src/dwf_mcp/instruments/i2c.py \
        src/dwf_mcp/instruments/spi.py src/dwf_mcp/instruments/uart.py \
        src/dwf_mcp/instruments/can.py
git commit -m "feat: add engine resource virtual pins to protocol instrument allocator claims"
```

---

## Task 2: DigitalIn observer claim + logic digital_in claim

`claim_observe(instrument)` reserves DigitalIn globally without blocking DigitalOut writers on the same physical pins. This enables sniff.spi (DigitalIn) and spi.configure (DigitalOut/protocol) to coexist.

**Files:**
- Modify: `src/dwf_mcp/allocator.py`
- Modify: `src/dwf_mcp/instruments/logic.py`

- [ ] **Step 2.1: Add `_observe_claims` and `claim_observe` to PinAllocator**

Replace `src/dwf_mcp/allocator.py` entirely:

```python
from __future__ import annotations

from dataclasses import dataclass, field


class PinAllocationError(Exception):
    """Raised when an instrument tries to claim pins already in use, or a resource group conflict."""


@dataclass(frozen=True)
class ResourceGroup:
    name: str
    pins: frozenset[str]
    exclusive: bool = True

    def __init__(self, name: str, pins: set[str] | frozenset[str], exclusive: bool = True) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "pins", frozenset(pins))
        object.__setattr__(self, "exclusive", exclusive)


@dataclass
class PinAllocator:
    resource_groups: list[ResourceGroup] = field(default_factory=list)
    _claims: dict[str, list[str]] = field(default_factory=dict)
    _observe_claims: set[str] = field(default_factory=set)  # instruments with DigitalIn observer claim

    def claim(self, instrument: str, pins: list[str]) -> None:
        # Replacement semantics: re-claiming for the same instrument frees its old pins first.
        self._claims.pop(instrument, None)
        pin_owners = self.claimed_pins()
        for pin in pins:
            if pin in pin_owners:
                raise PinAllocationError(
                    f"{instrument} cannot claim {pin}: already held by {pin_owners[pin]}"
                )
        # "digital_in" virtual pin conflicts with any existing observer claim.
        if "digital_in" in pins and self._observe_claims:
            observers = ", ".join(sorted(self._observe_claims))
            raise PinAllocationError(
                f"{instrument} cannot claim DigitalIn: already held by observer(s) ({observers})"
            )
        for group in self.resource_groups:
            if not group.exclusive:
                continue
            requested_in_group = group.pins & set(pins)
            if not requested_in_group:
                continue
            for other_instr, other_pins in self._claims.items():
                if other_instr == instrument:
                    continue
                if set(other_pins) & group.pins:
                    raise PinAllocationError(
                        f"{instrument} cannot claim {sorted(requested_in_group)}: "
                        f"resource group {group.name!r} is held by {other_instr}"
                    )
        self._claims[instrument] = list(pins)

    def claim_observe(self, instrument: str) -> None:
        """Reserve DigitalIn as a read-only observer.
        Does not conflict with DigitalOut writers on the same physical pins.
        Conflicts with any exclusive DigitalIn claim or another observer."""
        pin_owners = self.claimed_pins()
        if "digital_in" in pin_owners:
            raise PinAllocationError(
                f"{instrument} cannot observe DigitalIn: held exclusively by {pin_owners['digital_in']}"
            )
        if self._observe_claims:
            other = next(iter(self._observe_claims))
            raise PinAllocationError(
                f"{instrument} cannot observe DigitalIn: already observing ({other})"
            )
        self._observe_claims.add(instrument)

    def release(self, instrument: str) -> None:
        self._claims.pop(instrument, None)
        self._observe_claims.discard(instrument)

    def claimed_pins(self) -> dict[str, str]:
        return {pin: instr for instr, pins in self._claims.items() for pin in pins}

    def claimed_instruments(self) -> list[str]:
        return list(self._claims.keys())

    def clear(self) -> None:
        self._claims.clear()
        self._observe_claims.clear()
```

- [ ] **Step 2.2: Add "digital_in" to logic instrument claims**

In `src/dwf_mcp/instruments/logic.py`, find both `allocator.claim` calls:

`configure` method (around line 127):
```python
# Before:
self.device.allocator.claim("logic", pins)
# After:
self.device.allocator.claim("logic", ["digital_in"] + pins)
```

`record_start` method (around line 250):
```python
# Before:
self.device.allocator.claim("logic", pins)
# After:
self.device.allocator.claim("logic", ["digital_in"] + pins)
```

- [ ] **Step 2.3: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: `250 passed`

- [ ] **Step 2.4: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/allocator.py src/dwf_mcp/instruments/logic.py
git commit -m "feat: add claim_observe for DigitalIn observer and digital_in virtual pin"
```

---

## Task 3: ArtifactWriter.write_parquet

**Files:**
- Modify: `src/dwf_mcp/artifacts.py`
- Modify: `tests/unit/test_artifacts.py`

- [ ] **Step 3.1: Write failing test**

Add to `tests/unit/test_artifacts.py`:

```python
def test_write_parquet_creates_files(tmp_path: Path) -> None:
    writer = ArtifactWriter(workspace=tmp_path)
    records = [
        {"timestamp_s": 0.0, "error": False, "error_detail": None, "data": b"\xA5"},
        {"timestamp_s": 0.001, "error": True, "error_detail": "parity", "data": b"\x00"},
    ]
    result = writer.write_parquet("sniff_uart", records, config={"baud": 9600})
    assert Path(result.path).exists()
    assert result.path.endswith(".parquet")
    assert Path(result.sidecar_path).exists()
    assert result.summary["count"] == 2


def test_write_parquet_empty(tmp_path: Path) -> None:
    writer = ArtifactWriter(workspace=tmp_path)
    result = writer.write_parquet("sniff_can", [], config={})
    assert Path(result.path).exists()
    assert result.summary["count"] == 0


def test_write_parquet_roundtrip(tmp_path: Path) -> None:
    import pyarrow.parquet as pq
    writer = ArtifactWriter(workspace=tmp_path)
    records = [{"timestamp_s": 1.5, "frame_id": 0x123, "data": b"\x01\x02"}]
    result = writer.write_parquet("sniff_can", records, config={})
    table = pq.read_table(result.path)
    assert table.num_rows == 1
    assert table.column("frame_id")[0].as_py() == 0x123
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_artifacts.py -k "parquet" -v
```
Expected: `AttributeError: 'ArtifactWriter' object has no attribute 'write_parquet'`

- [ ] **Step 3.3: Implement write_parquet**

Add to `src/dwf_mcp/artifacts.py` after `write_npz`:

```python
def write_parquet(
    self,
    instrument: str,
    records: list[dict[str, Any]],
    config: dict[str, Any],
    output_path: Path | None = None,
    description: str | None = None,
) -> ArtifactResult:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if output_path is None:
        output_path = self._default_path(instrument, ".parquet")
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pylist(records) if records else pa.table({})
    pq.write_table(table, output_path)

    sidecar_path = output_path.with_suffix(".json")
    sidecar = {
        "instrument": instrument,
        "captured_at": datetime.now(UTC).isoformat(),
        "description": description,
        "config": config,
        "summary": {"count": len(records)},
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str))
    return ArtifactResult(
        path=str(output_path),
        sidecar_path=str(sidecar_path),
        summary={"count": len(records)},
    )
```

- [ ] **Step 3.4: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_artifacts.py -v
```
Expected: all pass

- [ ] **Step 3.5: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/artifacts.py tests/unit/test_artifacts.py
git commit -m "feat: add ArtifactWriter.write_parquet using pyarrow"
```

---

## Task 4: Decoder ABC + SpiTransaction

**Files:**
- Create: `src/dwf_mcp/instruments/decoder/base.py`
- Create: `src/dwf_mcp/instruments/decoder/__init__.py` (empty package marker for now)

- [ ] **Step 4.1: Create decoder package**

```bash
mkdir -p ~/work/dwf-mcp/dwf-mcp/src/dwf_mcp/instruments/decoder
touch ~/work/dwf-mcp/dwf-mcp/src/dwf_mcp/instruments/decoder/__init__.py
```

- [ ] **Step 4.2: Create base.py**

`src/dwf_mcp/instruments/decoder/base.py`:

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np


@dataclass
class SpiTransaction:
    timestamp_s: float
    word_index: int
    mosi: bytes
    miso: bytes | None       # None if no MISO pin captured
    cs_active: bool
    cs_error: bool           # CS deasserted mid-word
    error: bool = False
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "word_index": self.word_index,
            "mosi": self.mosi,
            "miso": self.miso,
            "cs_active": self.cs_active,
            "cs_error": self.cs_error,
            "error": self.error,
            "error_detail": self.error_detail,
        }


class Decoder(ABC):
    protocol_name: ClassVar[str]

    @abstractmethod
    def decode(
        self,
        samples: np.ndarray,      # (n_samples, 16) uint8
        pin_map: dict[str, int],  # signal name → column index
        **config: Any,
    ) -> list[Any]:
        ...
```

- [ ] **Step 4.3: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/instruments/decoder/
git commit -m "feat: add Decoder ABC and SpiTransaction dataclass"
```

---

## Task 5: SpiDecoder — TDD

**Files:**
- Create: `tests/unit/test_spi_decoder.py`
- Create: `src/dwf_mcp/instruments/decoder/spi.py`

- [ ] **Step 5.1: Write helper to generate synthetic SPI samples**

Create `tests/unit/test_spi_decoder.py`:

```python
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.spi import SpiDecoder


def _spi_samples(
    words: list[int],
    word_size: int = 8,
    bit_order: str = "msb",
    mode: int = 0,
    sph: int = 5,           # samples per half-clock-period
    with_miso: bool = True,
    with_cs: bool = True,
) -> tuple[np.ndarray, dict[str, int]]:
    """Build a synthetic (n, 16) uint8 SPI capture.

    Column layout: CLK=0, MOSI=1, MISO=2 (loopback = MOSI), CS=3 (active-low).
    mode 0 (CPOL=0,CPHA=0): idle CLK=0, sample on rising edge.
    mode 3 (CPOL=1,CPHA=1): idle CLK=1, sample on rising edge.
    """
    cpol = mode >> 1
    sample_on_rising = mode in (0, 3)  # modes 0 and 3 sample on rising edge

    rows: list[list[int]] = []

    def r(clk: int, mosi: int, miso: int, cs: int) -> list[int]:
        row = [0] * 16
        row[0] = clk
        row[1] = mosi
        row[2] = miso
        row[3] = cs
        return row

    # Idle before transfer
    for _ in range(2 * sph):
        rows.append(r(cpol, 0, 0, 1))

    for word_val in words:
        bits: list[int] = []
        for i in range(word_size):
            if bit_order == "msb":
                bits.append((word_val >> (word_size - 1 - i)) & 1)
            else:
                bits.append((word_val >> i) & 1)

        # CS asserts; first bit pre-loaded on MOSI (CPHA=0 standard)
        first_bit = bits[0]
        for _ in range(sph):
            rows.append(r(cpol, first_bit, first_bit, 0))

        for i, bit in enumerate(bits):
            active_clk = 1 - cpol
            # Sample edge
            for _ in range(sph):
                rows.append(r(active_clk, bit, bit, 0))
            # Return to idle clock; next bit pre-loaded
            next_bit = bits[i + 1] if i + 1 < len(bits) else 0
            for _ in range(sph):
                rows.append(r(cpol, next_bit, next_bit, 0))

        # CS deasserts
        for _ in range(sph):
            rows.append(r(cpol, 0, 0, 0))

    # Idle after transfer
    for _ in range(2 * sph):
        rows.append(r(cpol, 0, 0, 1))

    arr = np.array(rows, dtype=np.uint8)
    pin_map = {"clk": 0, "mosi": 1}
    if with_miso:
        pin_map["miso"] = 2
    if with_cs:
        pin_map["cs"] = 3
    return arr, pin_map


SAMPLE_RATE = 1_000_000.0  # 1 MHz


def test_mode0_single_byte():
    samples, pin_map = _spi_samples([0xA5])
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 1
    assert txns[0].mosi == bytes([0xA5])
    assert txns[0].miso == bytes([0xA5])   # loopback
    assert txns[0].cs_active is True
    assert txns[0].cs_error is False
    assert txns[0].word_index == 0


def test_mode0_two_bytes():
    samples, pin_map = _spi_samples([0xA5, 0x5A])
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 2
    assert txns[0].mosi == bytes([0xA5])
    assert txns[1].mosi == bytes([0x5A])
    assert txns[1].word_index == 1


def test_mode3_single_byte():
    samples, pin_map = _spi_samples([0x42], mode=3)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=3)
    assert len(txns) == 1
    assert txns[0].mosi == bytes([0x42])


def test_no_miso():
    samples, pin_map = _spi_samples([0xBE], with_miso=False)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 1
    assert txns[0].miso is None


def test_no_cs():
    samples, pin_map = _spi_samples([0xFF], with_cs=False)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 1
    assert txns[0].cs_active is True   # no CS means always active
    assert txns[0].cs_error is False


def test_timestamp_nonzero():
    samples, pin_map = _spi_samples([0x01])
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert txns[0].timestamp_s > 0.0


def test_lsb_first():
    # 0x01 LSB-first: bit0=1 is sent first → same decoded value
    samples, pin_map = _spi_samples([0x01], bit_order="lsb")
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0, bit_order="lsb")
    assert txns[0].mosi == bytes([0x01])
```

- [ ] **Step 5.2: Run tests to see them fail**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_spi_decoder.py -v 2>&1 | head -20
```
Expected: `ImportError: cannot import name 'SpiDecoder'`

- [ ] **Step 5.3: Implement SpiDecoder**

Create `src/dwf_mcp/instruments/decoder/spi.py`:

```python
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder, SpiTransaction

# (CPOL, CPHA) -> sample on rising edge?
_SAMPLE_ON_RISING: dict[tuple[int, int], bool] = {
    (0, 0): True,
    (0, 1): False,
    (1, 0): False,
    (1, 1): True,
}


class SpiDecoder(Decoder):
    protocol_name: ClassVar[str] = "spi"

    def decode(
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        mode: int = 0,
        bit_order: str = "msb",
        word_size: int = 8,
        **_: Any,
    ) -> list[SpiTransaction]:
        cpol = mode >> 1
        cpha = mode & 1
        sample_on_rising = _SAMPLE_ON_RISING[(cpol, cpha)]

        clk = samples[:, pin_map["clk"]]
        mosi = samples[:, pin_map["mosi"]]
        miso_col = pin_map.get("miso")
        cs_col = pin_map.get("cs")
        miso = samples[:, miso_col] if miso_col is not None else None
        cs = samples[:, cs_col] if cs_col is not None else None

        transactions: list[SpiTransaction] = []
        mosi_bits: list[int] = []
        miso_bits: list[int] = []
        word_index = 0

        n = len(samples)
        for i in range(1, n):
            prev_clk = int(clk[i - 1])
            curr_clk = int(clk[i])

            # CS deassertion mid-word check
            if cs is not None and mosi_bits:
                prev_cs_active = cs[i - 1] == 0
                curr_cs_active = cs[i] == 0
                if prev_cs_active and not curr_cs_active:
                    # CS deasserted with bits pending — emit error word
                    mosi_word, miso_word = _build_words(
                        mosi_bits, miso_bits, word_size, bit_order, miso is not None
                    )
                    transactions.append(SpiTransaction(
                        timestamp_s=i / sample_rate_hz,
                        word_index=word_index,
                        mosi=mosi_word,
                        miso=miso_word,
                        cs_active=True,
                        cs_error=True,
                        error=True,
                        error_detail="CS deasserted mid-word",
                    ))
                    word_index += 1
                    mosi_bits.clear()
                    miso_bits.clear()
                    continue

            # CLK edge
            rising = prev_clk == 0 and curr_clk == 1
            falling = prev_clk == 1 and curr_clk == 0
            if (sample_on_rising and rising) or (not sample_on_rising and falling):
                cs_active = (cs[i] == 0) if cs is not None else True
                if not cs_active and cs is not None:
                    continue  # ignore clocks while CS is deasserted
                mosi_bits.append(int(mosi[i]))
                if miso is not None:
                    miso_bits.append(int(miso[i]))

                if len(mosi_bits) == word_size:
                    mosi_word, miso_word = _build_words(
                        mosi_bits, miso_bits, word_size, bit_order, miso is not None
                    )
                    transactions.append(SpiTransaction(
                        timestamp_s=i / sample_rate_hz,
                        word_index=word_index,
                        mosi=mosi_word,
                        miso=miso_word,
                        cs_active=cs_active,
                        cs_error=False,
                    ))
                    word_index += 1
                    mosi_bits.clear()
                    miso_bits.clear()

        return transactions


def _build_words(
    mosi_bits: list[int],
    miso_bits: list[int],
    word_size: int,
    bit_order: str,
    has_miso: bool,
) -> tuple[bytes, bytes | None]:
    mosi_val = _bits_to_int(mosi_bits, word_size, bit_order)
    miso_val = _bits_to_int(miso_bits, word_size, bit_order) if has_miso and miso_bits else None
    return bytes([mosi_val]), bytes([miso_val]) if miso_val is not None else None


def _bits_to_int(bits: list[int], word_size: int, bit_order: str) -> int:
    val = 0
    if bit_order == "msb":
        for b in bits:
            val = (val << 1) | b
    else:
        for j, b in enumerate(bits):
            val |= b << j
    return val & ((1 << word_size) - 1)
```

- [ ] **Step 5.4: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_spi_decoder.py -v
```
Expected: all 7 tests pass

- [ ] **Step 5.5: Run full suite**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: passes (count increases by 7)

- [ ] **Step 5.6: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/instruments/decoder/ tests/unit/test_spi_decoder.py
git commit -m "feat: add SpiDecoder state machine with golden unit tests"
```

---

## Task 6: Backend ABC stubs + FakeBackend

**Files:**
- Modify: `src/dwf_mcp/backend.py`
- Modify: `src/dwf_mcp/backends/fake.py`

- [ ] **Step 6.1: Add ABC stubs to backend.py**

Add after the existing `can_receive` stub at the bottom of `src/dwf_mcp/backend.py`:

```python
# Sniff — stage 4.

def i2c_spy_start(self) -> None:
    raise NotImplementedError

def i2c_spy_status(self, max_data_size: int) -> tuple[int, int, list[int], int]:
    """Returns (start, stop, data_bytes, nak)."""
    raise NotImplementedError

def i2c_spy_stop(self) -> None:
    raise NotImplementedError

def uart_sniff(
    self,
    rx_pin_idx: int,
    baud: int,
    data_bits: int,
    parity: str,
    stop_bits: int,
    duration_s: float,
    poll_interval_s: float,
) -> list[tuple[float, bytes, bool]]:
    """Returns list of (timestamp_s, data, parity_error)."""
    raise NotImplementedError

def can_sniff(
    self,
    rx_pin_idx: int,
    bitrate: int,
    duration_s: float,
    poll_interval_s: float,
) -> list[tuple[float, int, bytes, bool, int]]:
    """Returns list of (timestamp_s, frame_id, data, extended, error_count)."""
    raise NotImplementedError
```

- [ ] **Step 6.2: Add FakeBackend stubs**

Add to `src/dwf_mcp/backends/fake.py` after the CAN stubs:

```python
# --- Sniff (stage 4) ---

def __init_sniff(self) -> None:
    # Called from __post_init__; adds sniff state to fake.
    self._i2c_spy_sequence: list[tuple[int, int, list[int], int]] = []
    self._i2c_spy_idx: int = 0
    self._uart_sniff_frames: list[tuple[float, bytes, bool]] = []
    self._can_sniff_frames: list[tuple[float, int, bytes, bool, int]] = []
    self.sniff_calls: list[tuple[str, dict]] = []
```

Wait, FakeBackend uses `@dataclass` — it has a `__post_init__`. Let me check the actual FakeBackend structure first, then add fields properly.

Actually looking at `fake.py`: it's a `@dataclass` with fields initialised at class level. Add the new sniff fields alongside existing ones:

In `src/dwf_mcp/backends/fake.py`, in the `FakeBackend` class definition, add fields after the CAN fields:

```python
# Sniff state
_i2c_spy_sequence: list[tuple[int, int, list[int], int]] = field(default_factory=list)
_i2c_spy_idx: int = 0
_uart_sniff_frames: list[tuple[float, bytes, bool]] = field(default_factory=list)
_can_sniff_frames: list[tuple[float, int, bytes, bool, int]] = field(default_factory=list)
sniff_calls: list[tuple[str, dict]] = field(default_factory=list)
```

Then add these methods:

```python
def i2c_spy_start(self) -> None:
    self.sniff_calls.append(("i2c_spy_start", {}))
    self._i2c_spy_idx = 0

def i2c_spy_status(self, max_data_size: int) -> tuple[int, int, list[int], int]:
    self.sniff_calls.append(("i2c_spy_status", {"max_data_size": max_data_size}))
    if self._i2c_spy_idx < len(self._i2c_spy_sequence):
        result = self._i2c_spy_sequence[self._i2c_spy_idx]
        self._i2c_spy_idx += 1
        return result
    return (0, 0, [], 0)  # no new data

def i2c_spy_stop(self) -> None:
    self.sniff_calls.append(("i2c_spy_stop", {}))

def uart_sniff(
    self, rx_pin_idx, baud, data_bits, parity, stop_bits, duration_s, poll_interval_s
) -> list[tuple[float, bytes, bool]]:
    self.sniff_calls.append(("uart_sniff", {"baud": baud}))
    return list(self._uart_sniff_frames)

def can_sniff(
    self, rx_pin_idx, bitrate, duration_s, poll_interval_s
) -> list[tuple[float, int, bytes, bool, int]]:
    self.sniff_calls.append(("can_sniff", {"bitrate": bitrate}))
    return list(self._can_sniff_frames)

# Test helpers
def set_i2c_spy_sequence(self, seq: list[tuple[int, int, list[int], int]]) -> None:
    self._i2c_spy_sequence = list(seq)
    self._i2c_spy_idx = 0

def set_uart_sniff_frames(self, frames: list[tuple[float, bytes, bool]]) -> None:
    self._uart_sniff_frames = list(frames)

def set_can_sniff_frames(self, frames: list[tuple[float, int, bytes, bool, int]]) -> None:
    self._can_sniff_frames = list(frames)
```

- [ ] **Step 6.3: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: all pass

- [ ] **Step 6.4: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/fake.py
git commit -m "feat: add sniff backend ABC stubs and FakeBackend implementations"
```

---

## Task 7: PydwfBackend sniff implementations

**Files:**
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`

- [ ] **Step 7.1: Add i2c_spy_start, i2c_spy_status, i2c_spy_stop**

Add after the existing `i2c_write_read` block:

```python
def i2c_spy_start(self) -> None:
    self._device.protocol.i2c.spyStart()

def i2c_spy_status(self, max_data_size: int) -> tuple[int, int, list[int], int]:
    start, stop, data, nak = self._device.protocol.i2c.spyStatus(max_data_size)
    return int(start), int(stop), [int(b) for b in data], int(nak)

def i2c_spy_stop(self) -> None:
    self._device.protocol.i2c.reset()
```

- [ ] **Step 7.2: Add uart_sniff**

Add after the `uart_read` block:

```python
def uart_sniff(
    self,
    rx_pin_idx: int,
    baud: int,
    data_bits: int,
    parity: str,
    stop_bits: int,
    duration_s: float,
    poll_interval_s: float,
) -> list[tuple[float, bytes, bool]]:
    import time
    uart = self._device.protocol.uart
    uart.reset()
    uart.rateSet(baud)
    uart.bitsSet(data_bits)
    parity_map = {"none": 0, "odd": 1, "even": 2}
    uart.paritySet(parity_map[parity])
    uart.stopSet(stop_bits)
    uart.rxSet(rx_pin_idx)
    uart.rx(0)
    uart.rx(1)
    try:
        frames: list[tuple[float, bytes, bool]] = []
        start_t = time.monotonic()
        deadline = start_t + duration_s
        while time.monotonic() < deadline:
            rx_data, pe = uart.rx(256)
            if rx_data:
                ts = time.monotonic() - start_t
                frames.append((ts, bytes(rx_data), bool(pe)))
            else:
                time.sleep(poll_interval_s)
        return frames
    finally:
        uart.reset()
```

- [ ] **Step 7.3: Add can_sniff**

Add after the `can_receive` block:

```python
def can_sniff(
    self,
    rx_pin_idx: int,
    bitrate: int,
    duration_s: float,
    poll_interval_s: float,
) -> list[tuple[float, int, bytes, bool, int]]:
    import time
    can = self._device.protocol.can
    can.reset()
    can.rateSet(bitrate)
    can.rxSet(rx_pin_idx)
    try:
        frames: list[tuple[float, int, bytes, bool, int]] = []
        start_t = time.monotonic()
        deadline = start_t + duration_s
        while time.monotonic() < deadline:
            frame_id, ext, _remote, data, status = can.rx()
            if status:
                ts = time.monotonic() - start_t
                frames.append((ts, int(frame_id), bytes(data), bool(ext), int(status)))
            else:
                time.sleep(poll_interval_s)
        return frames
    finally:
        can.reset()
```

- [ ] **Step 7.4: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: all pass (pydwf backend not exercised in unit tests)

- [ ] **Step 7.5: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/backends/pydwf_backend.py
git commit -m "feat: implement i2c_spy, uart_sniff, can_sniff in PydwfBackend"
```

---

## Task 8: Sniff instrument — i2c, uart, can

**Files:**
- Create: `src/dwf_mcp/instruments/sniff.py`
- Create: `tests/unit/test_sniff.py`

- [ ] **Step 8.1: Write failing tests for sniff.i2c, sniff.uart, sniff.can**

Create `tests/unit/test_sniff.py`:

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.sniff import Sniff
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
def sniff(device: DwfDevice, tmp_path: Path) -> Sniff:
    device.open()
    return Sniff(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


# --- sniff.uart ---

def test_sniff_uart_calls_backend(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_uart_sniff_frames([(0.001, b"\x41", False), (0.002, b"\x42", False)])

    async def run() -> dict:
        return await sniff.uart(
            rx_pin="dio0", baud=9600, duration_s=0.01,
            data_bits=8, parity="none", stop_bits=1,
        )

    result = asyncio.run(run())
    assert result["count"] == 2
    assert result["error_count"] == 0
    assert result["artifact_path"] is not None
    calls = [c[0] for c in fake.sniff_calls]
    assert "uart_sniff" in calls


def test_sniff_uart_parity_errors(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_uart_sniff_frames([(0.001, b"\xFF", True)])  # parity error

    async def run() -> dict:
        return await sniff.uart(rx_pin="dio0", baud=9600, duration_s=0.01)

    result = asyncio.run(run())
    assert result["error_count"] == 1


def test_sniff_uart_releases_pins(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore

    async def run() -> dict:
        return await sniff.uart(rx_pin="dio1", baud=115200, duration_s=0.01)

    asyncio.run(run())
    assert "sniff_uart" not in sniff.device.allocator.claimed_instruments()


# --- sniff.can ---

def test_sniff_can_calls_backend(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_can_sniff_frames([(0.001, 0x123, b"\x01\x02", False, 0)])

    async def run() -> dict:
        return await sniff.can(rx_pin="dio0", bitrate=500_000, duration_s=0.01)

    result = asyncio.run(run())
    assert result["count"] == 1
    assert result["artifact_path"] is not None


# --- sniff.i2c ---

def test_sniff_i2c_assembles_write_transaction(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    # Simulate: START + address byte (0x50 write = 0xA0) + data byte (0x01) + STOP
    fake.set_i2c_spy_sequence([
        (1, 0, [0xA0, 0x01], 0),  # start=1, stop=0, data=[addr_byte, data], nak=0
        (0, 1, [], 0),             # start=0, stop=1 (transaction ends)
    ])

    async def run() -> dict:
        return await sniff.i2c(
            sda_pin="dio0", scl_pin="dio1", duration_s=0.02, poll_interval_s=0.001
        )

    result = asyncio.run(run())
    assert result["count"] == 1
    assert result["artifact_path"] is not None
    # Verify parquet content
    import pyarrow.parquet as pq
    table = pq.read_table(result["artifact_path"])
    assert table.num_rows == 1
    assert table.column("type")[0].as_py() == "write"
    assert table.column("address")[0].as_py() == 0x50   # 0xA0 >> 1


def test_sniff_i2c_releases_pins(sniff: Sniff) -> None:
    async def run() -> dict:
        return await sniff.i2c(sda_pin="dio0", scl_pin="dio1", duration_s=0.01)

    asyncio.run(run())
    assert "sniff_i2c" not in sniff.device.allocator.claimed_instruments()
```

- [ ] **Step 8.2: Run tests to see them fail**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_sniff.py -v 2>&1 | head -10
```
Expected: `ImportError: cannot import name 'Sniff'`

- [ ] **Step 8.3: Implement Sniff instrument (i2c/uart/can)**

Create `src/dwf_mcp/instruments/sniff.py`:

```python
"""Sniff instrument: passive protocol capture using hardware protocol engines."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

log = logging.getLogger(__name__)

_PIN_RE = r"^dio([0-9]|1[0-5])$"


def _dio_index(pin: str) -> int:
    return int(pin[3:])


SNIFF_I2C_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sda_pin", "scl_pin", "duration_s"],
    "properties": {
        "sda_pin": {"type": "string", "pattern": _PIN_RE},
        "scl_pin": {"type": "string", "pattern": _PIN_RE},
        "duration_s": {"type": "number", "minimum": 0.001},
        "clock_hz": {"type": "number", "default": 400000},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SNIFF_UART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rx_pin", "baud", "duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "baud": {"type": "integer", "minimum": 300},
        "duration_s": {"type": "number", "minimum": 0.001},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {"type": "string", "enum": ["none", "odd", "even"], "default": "none"},
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SNIFF_CAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rx_pin", "bitrate", "duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "bitrate": {"type": "integer", "minimum": 10_000},
        "duration_s": {"type": "number", "minimum": 0.001},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SPI_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["clk_pin", "mosi_pin", "mode", "freq_hz"],
    "properties": {
        "clk_pin": {"type": "string", "pattern": _PIN_RE},
        "mosi_pin": {"type": "string", "pattern": _PIN_RE},
        "miso_pin": {"type": "string", "pattern": _PIN_RE},
        "cs_pin": {"type": "string", "pattern": _PIN_RE},
        "mode": {"type": "integer", "enum": [0, 1, 2, 3]},
        "freq_hz": {"type": "number", "minimum": 1.0},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SPI_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sniff_id"],
    "properties": {"sniff_id": {"type": "string"}},
}

SPI_STOP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sniff_id"],
    "properties": {"sniff_id": {"type": "string"}},
}


class Sniff(Instrument):
    name = "sniff"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "i2c":       ("i2c",        SNIFF_I2C_SCHEMA),
        "uart":      ("uart",       SNIFF_UART_SCHEMA),
        "can":       ("can",        SNIFF_CAN_SCHEMA),
        "spi_start": ("spi_start",  SPI_START_SCHEMA),
        "spi_status":("spi_status", SPI_STATUS_SCHEMA),
        "spi_stop":  ("spi_stop",   SPI_STOP_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._spi_sessions: dict[str, Any] = {}  # sniff_id -> RecordingSession

    # --- sniff.i2c ---

    async def i2c(
        self,
        sda_pin: str,
        scl_pin: str,
        duration_s: float,
        clock_hz: float = 400_000,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        import time
        self.device.allocator.claim("sniff_i2c", ["i2c_engine", sda_pin, scl_pin])
        transactions: list[dict[str, Any]] = []
        error_count = 0
        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            self.device.backend.i2c_spy_start()
            deadline = time.monotonic() + duration_s
            pending_bytes: list[int] = []
            in_transaction = False

            while time.monotonic() < deadline:
                await asyncio.sleep(poll_interval_s)
                start, stop, data, nak = self.device.backend.i2c_spy_status(256)

                if start:
                    if in_transaction and pending_bytes:
                        # Repeated START: close current transaction
                        _close_i2c_transaction(pending_bytes, nak, transactions)
                        if pending_bytes and nak:
                            error_count += 1
                    pending_bytes.clear()
                    in_transaction = True

                if data:
                    pending_bytes.extend(data)

                if stop and in_transaction:
                    _close_i2c_transaction(pending_bytes, nak, transactions)
                    if nak:
                        error_count += 1
                    pending_bytes.clear()
                    in_transaction = False

            try:
                result = self.artifacts.write_parquet(
                    "sniff_i2c",
                    transactions,
                    config={
                        "sda_pin": sda_pin, "scl_pin": scl_pin,
                        "clock_hz": clock_hz, "duration_s": duration_s,
                        "poll_interval_s": poll_interval_s,
                    },
                    output_path=output_path,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.i2c artifact write failed")
                artifact_error = str(exc)
        finally:
            with suppress(Exception):
                self.device.backend.i2c_spy_stop()
            self.device.allocator.release("sniff_i2c")

        return {
            "artifact_path": artifact_path,
            "sidecar_path": artifact_path.replace(".parquet", ".json") if artifact_path else None,
            "count": len(transactions),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {"first_n": [_summarise_i2c(t) for t in transactions[:5]]},
        }

    # --- sniff.uart ---

    async def uart(
        self,
        rx_pin: str,
        baud: int,
        duration_s: float,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        self.device.allocator.claim("sniff_uart", ["uart_engine", rx_pin])
        artifact_path: str | None = None
        artifact_error: str | None = None
        raw_frames: list[tuple[float, bytes, bool]] = []
        error_count = 0
        try:
            raw_frames = self.device.backend.uart_sniff(
                rx_pin_idx=_dio_index(rx_pin),
                baud=baud,
                data_bits=data_bits,
                parity=parity,
                stop_bits=stop_bits,
                duration_s=duration_s,
                poll_interval_s=poll_interval_s,
            )
            error_count = sum(1 for _, _, pe in raw_frames if pe)
            records = [
                {
                    "timestamp_s": ts,
                    "data": data,
                    "parity_error": pe,
                    "framing_error": None,
                    "break_condition": None,
                    "error": pe,
                    "error_detail": "parity error" if pe else None,
                }
                for ts, data, pe in raw_frames
            ]
            try:
                result = self.artifacts.write_parquet(
                    "sniff_uart", records,
                    config={
                        "rx_pin": rx_pin, "baud": baud, "data_bits": data_bits,
                        "parity": parity, "stop_bits": stop_bits,
                        "duration_s": duration_s, "poll_interval_s": poll_interval_s,
                    },
                    output_path=output_path,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.uart artifact write failed")
                artifact_error = str(exc)
        finally:
            self.device.allocator.release("sniff_uart")

        return {
            "artifact_path": artifact_path,
            "sidecar_path": artifact_path.replace(".parquet", ".json") if artifact_path else None,
            "count": len(raw_frames),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {},
        }

    # --- sniff.can ---

    async def can(
        self,
        rx_pin: str,
        bitrate: int,
        duration_s: float,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        self.device.allocator.claim("sniff_can", ["can_engine", rx_pin])
        artifact_path: str | None = None
        artifact_error: str | None = None
        raw_frames: list[tuple[float, int, bytes, bool, int]] = []
        error_count = 0
        records: list[dict[str, Any]] = []
        try:
            raw_frames = self.device.backend.can_sniff(
                rx_pin_idx=_dio_index(rx_pin),
                bitrate=bitrate,
                duration_s=duration_s,
                poll_interval_s=poll_interval_s,
            )
            error_count = sum(1 for *_, ec in raw_frames if ec)
            records = [  # noqa: assigned before try block but filled here
                {
                    "timestamp_s": ts,
                    "frame_id": fid,
                    "extended": ext,
                    "rtr": False,
                    "dlc": len(data),
                    "data": data,
                    "crc_valid": None,
                    "ack_received": None,
                    "error_type": None,
                    "error": bool(ec),
                    "error_detail": f"error_count={ec}" if ec else None,
                }
                for ts, fid, data, ext, ec in raw_frames
            ]
            try:
                result = self.artifacts.write_parquet(
                    "sniff_can", records,
                    config={"rx_pin": rx_pin, "bitrate": bitrate, "duration_s": duration_s},
                    output_path=output_path,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.can artifact write failed")
                artifact_error = str(exc)
        finally:
            self.device.allocator.release("sniff_can")

        return {
            "artifact_path": artifact_path,
            "sidecar_path": artifact_path.replace(".parquet", ".json") if artifact_path else None,
            "count": len(records),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {},
        }

    # --- spi_start / spi_status / spi_stop (Task 9) ---

    async def spi_start(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("implement in Task 9")

    def spi_status(self, sniff_id: str) -> dict[str, Any]:
        raise NotImplementedError("implement in Task 9")

    async def spi_stop(self, sniff_id: str) -> dict[str, Any]:
        raise NotImplementedError("implement in Task 9")

    def release(self) -> None:
        self.device.allocator.release("sniff_i2c")
        self.device.allocator.release("sniff_uart")
        self.device.allocator.release("sniff_can")
        # spi sessions released in spi_stop; best-effort cleanup here
        for sniff_id in list(self._spi_sessions):
            with suppress(Exception):
                self.device.backend.logic_record_stop()
            self.device.allocator.release(f"sniff_spi_{sniff_id}")
        self._spi_sessions.clear()


def _close_i2c_transaction(
    pending_bytes: list[int], nak: int, out: list[dict[str, Any]]
) -> None:
    if not pending_bytes:
        return
    addr_byte = pending_bytes[0]
    address = addr_byte >> 1
    direction = "read" if (addr_byte & 1) else "write"
    data = bytes(pending_bytes[1:])
    # NAK encoding: verify empirically at implementation time (TODO per spec).
    # For now store raw nak value; nak_at_byte refinement deferred.
    nak_at_byte = nak if nak else None
    out.append({
        "timestamp_s": 0.0,  # approximate; i2c_spy doesn't provide per-byte timestamps
        "type": direction,
        "address": address,
        "address_bits": 7,
        "data": data,
        "nak_at_byte": nak_at_byte,
        "error": bool(nak),
        "error_detail": f"nak at byte {nak_at_byte}" if nak_at_byte is not None else None,
    })


def _summarise_i2c(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": t["type"],
        "address": hex(t["address"]),
        "data_len": len(t["data"]) if t["data"] else 0,
    }
```

- [ ] **Step 8.4: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_sniff.py -v
```
Expected: all tests pass

- [ ] **Step 8.5: Run full suite**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: all pass

- [ ] **Step 8.6: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/instruments/sniff.py tests/unit/test_sniff.py
git commit -m "feat: add Sniff instrument for i2c/uart/can with unit tests"
```

---

## Task 9: Sniff.spi (start/status/stop)

**Files:**
- Modify: `src/dwf_mcp/instruments/sniff.py`
- Modify: `tests/unit/test_sniff.py`

- [ ] **Step 9.1: Add failing tests for spi_start/status/stop**

Add to `tests/unit/test_sniff.py`:

```python
# --- sniff.spi_start / spi_status / spi_stop ---

def test_spi_start_returns_sniff_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    # Return one chunk then done
    fake.set_logic_record_status_sequence([(10, 0, 1), (0, 0, 0)])
    fake._logic_record_canned_chunk = _make_spi_chunk(0xA5)

    async def run() -> dict:
        return await sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000
        )

    result = asyncio.run(run())
    assert "sniff_id" in result
    assert isinstance(result["sniff_id"], str)
    # Cleanup
    asyncio.run(sniff.spi_stop(result["sniff_id"]))


def test_spi_status_returns_sample_count(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(10, 0, 1)])
    fake._logic_record_canned_chunk = _make_spi_chunk(0xA5)

    async def run() -> tuple[dict, dict]:
        start_result = await sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000
        )
        await asyncio.sleep(0.02)
        status = sniff.spi_status(start_result["sniff_id"])
        await sniff.spi_stop(start_result["sniff_id"])
        return start_result, status

    _, status = asyncio.run(run())
    assert "samples_received" in status
    assert "lost_samples" in status


def test_spi_stop_produces_artifact(sniff: Sniff, tmp_path: Path) -> None:
    from dwf_mcp.instruments.decoder.spi import SpiDecoder
    from tests.unit.test_spi_decoder import _spi_samples

    samples, _ = _spi_samples([0xA5, 0x5A])
    fake: FakeBackend = sniff.device.backend  # type: ignore
    # Feed the samples as a single chunk
    fake._logic_record_canned_chunk = samples
    fake.set_logic_record_status_sequence([(len(samples), 0, 1), (0, 0, 0)])

    async def run() -> dict:
        start = await sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3",
            mode=0, freq_hz=100_000,
        )
        await asyncio.sleep(0.05)
        return await sniff.spi_stop(start["sniff_id"])

    result = asyncio.run(run())
    assert result["artifact_path"] is not None
    assert result["count"] == 2   # two words decoded


def _make_spi_chunk(byte_val: int, sph: int = 5) -> "np.ndarray":
    """Minimal synthetic SPI chunk for FakeBackend canned data."""
    import numpy as np
    from tests.unit.test_spi_decoder import _spi_samples
    samples, _ = _spi_samples([byte_val], sph=sph)
    return samples
```

- [ ] **Step 9.2: Implement spi_start/status/stop in sniff.py**

Replace the stub `spi_start`, `spi_status`, `spi_stop` methods in `src/dwf_mcp/instruments/sniff.py`:

```python
async def spi_start(
    self,
    clk_pin: str,
    mosi_pin: str,
    mode: int,
    freq_hz: float,
    miso_pin: str | None = None,
    cs_pin: str | None = None,
    poll_interval_s: float = 0.010,
    output_path: str | None = None,
) -> dict[str, Any]:
    from dwf_mcp.streaming import RecordingSession, record_loop

    sample_rate_hz = freq_hz * 10  # 10× oversampling rule of thumb
    pins = [p for p in [clk_pin, mosi_pin, miso_pin, cs_pin] if p is not None]

    sniff_id = str(uuid.uuid4())
    allocator_key = f"sniff_spi_{sniff_id}"
    self.device.allocator.claim_observe(allocator_key)
    try:
        # Use a large sentinel duration; spi_stop terminates capture explicitly.
        self.device.backend.logic_record_configure(
            pin_mask=_pins_to_mask(pins),
            sample_rate_hz=sample_rate_hz,
            duration_s=3600.0,
        )
        self.device.backend.logic_record_arm()
    except Exception:
        with suppress(Exception):
            self.device.backend.logic_record_stop()
        self.device.allocator.release(allocator_key)
        raise

    session = RecordingSession(
        record_id=sniff_id,
        task=None,
        notification_task=None,
        queue=asyncio.Queue(maxsize=32),
        chunks=[],
        lost_samples=0,
        done=False,
        meta={
            "pins": pins,
            "sample_rate_hz": sample_rate_hz,
            "clk_pin": clk_pin,
            "mosi_pin": mosi_pin,
            "miso_pin": miso_pin,
            "cs_pin": cs_pin,
            "mode": mode,
            "output_path": output_path,
            "allocator_key": allocator_key,
        },
    )
    session.task = asyncio.create_task(
        record_loop(
            session,
            self.device.backend.logic_record_status,
            self.device.backend.logic_record_read,
        )
    )
    self._spi_sessions[sniff_id] = session
    return {"sniff_id": sniff_id}


def spi_status(self, sniff_id: str) -> dict[str, Any]:
    session = self._spi_sessions.get(sniff_id)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")
    total_samples = sum(len(c) for c in session.chunks)
    return {"samples_received": total_samples, "lost_samples": session.lost_samples}


async def spi_stop(self, sniff_id: str) -> dict[str, Any]:
    from contextlib import suppress
    import numpy as np
    from dwf_mcp.instruments.decoder.spi import SpiDecoder

    session = self._spi_sessions.pop(sniff_id, None)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")

    artifact_path: str | None = None
    artifact_error: str | None = None
    try:
        # 1. Cancel background task
        if session.task is not None:
            session.task.cancel()
            with suppress(asyncio.CancelledError):
                await session.task

        # 2. Stop hardware
        with suppress(Exception):
            self.device.backend.logic_record_stop()

        # 3. Drain remaining samples (mirror logic.record_stop contract)
        try:
            available, lost, _ = self.device.backend.logic_record_status()
            session.lost_samples += lost
            if available > 0:
                chunk = self.device.backend.logic_record_read(available)
                session.chunks.append(chunk)
        except Exception as exc:
            log.warning("spi_stop drain failed: %s", exc)

        # 4. Decode
        count = 0
        error_count = 0
        if session.chunks:
            try:
                all_samples = np.concatenate(session.chunks, axis=0)
                meta = session.meta
                pins = meta["pins"]
                pin_map = {
                    "clk":  pins.index(meta["clk_pin"]),
                    "mosi": pins.index(meta["mosi_pin"]),
                }
                if meta["miso_pin"] and meta["miso_pin"] in pins:
                    pin_map["miso"] = pins.index(meta["miso_pin"])
                if meta["cs_pin"] and meta["cs_pin"] in pins:
                    pin_map["cs"] = pins.index(meta["cs_pin"])

                decoder = SpiDecoder()
                txns = decoder.decode(
                    all_samples, pin_map,
                    sample_rate_hz=meta["sample_rate_hz"],
                    mode=meta["mode"],
                )
                count = len(txns)
                error_count = sum(1 for t in txns if t.error)
                records = [t.to_dict() for t in txns]
                result = self.artifacts.write_parquet(
                    "sniff_spi", records,
                    config={k: v for k, v in meta.items() if k != "allocator_key"},
                    output_path=meta.get("output_path"),
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("spi_stop decode/write failed for sniff_id=%r", sniff_id)
                artifact_error = str(exc)
    finally:
        # 5. Release allocator
        self.device.allocator.release(session.meta["allocator_key"])

    return {
        "artifact_path": artifact_path,
        "sidecar_path": artifact_path.replace(".parquet", ".json") if artifact_path else None,
        "count": count,
        "error_count": error_count,
        "lost_samples": session.lost_samples,
        "artifact_error": artifact_error,
        "summary": {},
    }
```

Also add helper `_pins_to_mask` (reuse from logic.py pattern) at the top of `sniff.py`:

```python
def _pins_to_mask(pins: list[str]) -> int:
    mask = 0
    for p in pins:
        mask |= 1 << int(p[3:])
    return mask
```

- [ ] **Step 9.3: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_sniff.py -v
```
Expected: all pass

- [ ] **Step 9.4: Run full suite**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: all pass

- [ ] **Step 9.5: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/instruments/sniff.py tests/unit/test_sniff.py
git commit -m "feat: implement sniff.spi_start/status/stop with DigitalIn record path"
```

---

## Task 10: decoder.spi instrument

**Files:**
- Modify: `src/dwf_mcp/instruments/decoder/__init__.py`
- Create (new): `tests/unit/test_decoder.py`

- [ ] **Step 10.1: Write failing test**

Create `tests/unit/test_decoder.py`:

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.decoder import Decoder as DecoderInstrument
from dwf_mcp.policy import SafetyPolicy
from tests.unit.test_spi_decoder import _spi_samples


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
def decoder(device: DwfDevice, tmp_path: Path) -> DecoderInstrument:
    device.open()
    return DecoderInstrument(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def _write_npz_with_sidecar(samples: np.ndarray, pins: list[str], sr: float, out: Path) -> Path:
    """Write a synthetic npz + sidecar in the format logic.capture produces."""
    import json
    from datetime import UTC, datetime

    npz_path = out / "capture.npz"
    arrays = {p: samples[:, i] for i, p in enumerate(pins)}
    np.savez_compressed(npz_path, **arrays)
    sidecar = {
        "instrument": "logic",
        "captured_at": datetime.now(UTC).isoformat(),
        "config": {"pins": pins, "sample_rate_hz": sr},
        "summary": {"sample_count": len(samples), "sample_rate_hz": sr},
    }
    npz_path.with_suffix(".json").write_text(json.dumps(sidecar))
    return npz_path


def test_decoder_spi_decodes_known_data(decoder: DecoderInstrument, tmp_path: Path) -> None:
    samples, _ = _spi_samples([0xA5, 0x5A])
    pins = ["dio0", "dio1", "dio2", "dio3"]  # clk, mosi, miso, cs
    npz_path = _write_npz_with_sidecar(samples, pins, 1_000_000.0, tmp_path)

    async def run() -> dict:
        return await decoder.spi(
            capture_path=str(npz_path),
            clk_pin="dio0", mosi_pin="dio1",
            miso_pin="dio2", cs_pin="dio3",
            mode=0,
        )

    result = asyncio.run(run())
    assert result["count"] == 2
    assert result["artifact_path"] is not None

    import pyarrow.parquet as pq
    table = pq.read_table(result["artifact_path"])
    mosi_col = [row.as_py() for row in table.column("mosi")]
    assert bytes([0xA5]) in mosi_col
    assert bytes([0x5A]) in mosi_col


def test_decoder_spi_missing_pin_returns_error(decoder: DecoderInstrument, tmp_path: Path) -> None:
    samples, _ = _spi_samples([0xFF])
    npz_path = _write_npz_with_sidecar(samples, ["dio0", "dio1"], 1_000_000.0, tmp_path)

    async def run() -> dict:
        return await decoder.spi(
            capture_path=str(npz_path),
            clk_pin="dio0", mosi_pin="dio5",  # dio5 not captured
        )

    result = asyncio.run(run())
    assert "error" in result


def test_decoder_spi_missing_sample_rate_returns_error(
    decoder: DecoderInstrument, tmp_path: Path
) -> None:
    samples, _ = _spi_samples([0x01])
    npz_path = tmp_path / "bad.npz"
    np.savez_compressed(npz_path, dio0=samples[:, 0], dio1=samples[:, 1])
    # Sidecar without sample_rate_hz
    sidecar = {"config": {"pins": ["dio0", "dio1"]}}
    npz_path.with_suffix(".json").write_text(json.dumps(sidecar))

    async def run() -> dict:
        return await decoder.spi(capture_path=str(npz_path), clk_pin="dio0", mosi_pin="dio1")

    result = asyncio.run(run())
    assert "error" in result
```

- [ ] **Step 10.2: Run test to see it fail**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_decoder.py -v 2>&1 | head -10
```
Expected: `ImportError: cannot import name 'Decoder'`

- [ ] **Step 10.3: Implement Decoder instrument**

Replace `src/dwf_mcp/instruments/decoder/__init__.py`:

```python
"""Decoder instrument: post-process logic capture artifacts."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

log = logging.getLogger(__name__)

DECODER_SPI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["capture_path", "clk_pin", "mosi_pin"],
    "properties": {
        "capture_path": {"type": "string"},
        "clk_pin":  {"type": "string"},
        "mosi_pin": {"type": "string"},
        "miso_pin": {"type": "string"},
        "cs_pin":   {"type": "string"},
        "mode":     {"type": "integer", "enum": [0, 1, 2, 3], "default": 0},
        "bit_order":{"type": "string", "enum": ["msb", "lsb"], "default": "msb"},
        "word_size":{"type": "integer", "minimum": 1, "maximum": 32, "default": 8},
        "output_path": {"type": "string"},
    },
}


class Decoder(Instrument):
    name = "decoder"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "spi": ("spi", DECODER_SPI_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts

    async def spi(
        self,
        capture_path: str,
        clk_pin: str,
        mosi_pin: str,
        miso_pin: str | None = None,
        cs_pin: str | None = None,
        mode: int = 0,
        bit_order: str = "msb",
        word_size: int = 8,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        from dwf_mcp.instruments.decoder.spi import SpiDecoder

        npz_path = Path(capture_path)
        sidecar_path = npz_path.with_suffix(".json")

        # Load sidecar for pin list and sample_rate_hz
        try:
            sidecar = json.loads(sidecar_path.read_text())
        except Exception as exc:
            return {"error": f"cannot read sidecar {sidecar_path}: {exc}"}

        config = sidecar.get("config", sidecar.get("summary", {}))
        captured_pins: list[str] = config.get("pins", [])
        sample_rate_hz = config.get("sample_rate_hz")

        if sample_rate_hz is None:
            return {"error": "sidecar missing sample_rate_hz; cannot compute timestamps"}

        # Validate requested pins exist
        for label, pin in [("clk_pin", clk_pin), ("mosi_pin", mosi_pin)]:
            if pin not in captured_pins:
                return {"error": f"{label}={pin!r} was not captured; available: {captured_pins}"}
        if miso_pin and miso_pin not in captured_pins:
            return {"error": f"miso_pin={miso_pin!r} was not captured; available: {captured_pins}"}
        if cs_pin and cs_pin not in captured_pins:
            return {"error": f"cs_pin={cs_pin!r} was not captured; available: {captured_pins}"}

        # Build (n_samples, 16) array from npz columns
        data = np.load(npz_path)
        n = len(data[captured_pins[0]])
        samples = np.zeros((n, 16), dtype=np.uint8)
        for pin in captured_pins:
            col = int(pin[3:])  # "dio3" → 3
            samples[:, col] = data[pin]

        pin_map: dict[str, int] = {
            "clk": int(clk_pin[3:]),
            "mosi": int(mosi_pin[3:]),
        }
        if miso_pin:
            pin_map["miso"] = int(miso_pin[3:])
        if cs_pin:
            pin_map["cs"] = int(cs_pin[3:])

        decoder = SpiDecoder()
        txns = decoder.decode(
            samples, pin_map,
            sample_rate_hz=float(sample_rate_hz),
            mode=mode, bit_order=bit_order, word_size=word_size,
        )
        error_count = sum(1 for t in txns if t.error)
        records = [t.to_dict() for t in txns]

        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            result = self.artifacts.write_parquet(
                "decoder_spi", records,
                config={
                    "capture_path": capture_path, "clk_pin": clk_pin,
                    "mosi_pin": mosi_pin, "miso_pin": miso_pin, "cs_pin": cs_pin,
                    "mode": mode, "bit_order": bit_order, "word_size": word_size,
                    "sample_rate_hz": sample_rate_hz,
                },
                output_path=output_path,
            )
            artifact_path = result.path
        except Exception as exc:
            log.exception("decoder.spi artifact write failed")
            artifact_error = str(exc)

        return {
            "artifact_path": artifact_path,
            "sidecar_path": artifact_path.replace(".parquet", ".json") if artifact_path else None,
            "count": len(txns),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {"first_n": [t.to_dict() for t in txns[:5]]},
        }

    def release(self) -> None:
        pass
```

- [ ] **Step 10.4: Run tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest tests/unit/test_decoder.py -v
```
Expected: all 3 tests pass

- [ ] **Step 10.5: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/instruments/decoder/__init__.py tests/unit/test_decoder.py
git commit -m "feat: implement decoder.spi tool with sidecar-based pin resolution"
```

---

## Task 11: Register instruments + full suite check

**Files:**
- Modify: `src/dwf_mcp/server.py`

- [ ] **Step 11.1: Add imports and register calls**

In `src/dwf_mcp/server.py`, add imports after the existing instrument imports:

```python
from dwf_mcp.instruments.sniff import Sniff
from dwf_mcp.instruments.decoder import Decoder as DecoderInstrument
```

In `build_app()`, after `app.register_instrument(CAN)`:

```python
app.register_instrument(Sniff)
app.register_instrument(DecoderInstrument)
```

- [ ] **Step 11.2: Verify server.py `_make_instrument_handler` handles spi_start correctly**

`sniff.spi_start` is named `spi_start`, not `record_start`. The special-case in `server.py:116` only fires for `method_name == "record_start"`. `spi_start` doesn't need `on_chunk` injection — confirm the handler works as-is:

```python
# In _make_instrument_handler, the existing logic is:
if method_name == "record_start" and on_record_chunk is not None:
    kwargs["on_chunk"] = on_record_chunk
```

`spi_start` is a different name — no special handling needed. It will be called as a plain coroutine. ✓

- [ ] **Step 11.3: Run full test suite**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q && ruff check src/ tests/
```
Expected: all tests pass, ruff clean (fix any new linting issues before committing)

- [ ] **Step 11.4: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add src/dwf_mcp/server.py
git commit -m "feat: register Sniff and Decoder instruments in build_app"
```

---

## Task 12: Hardware tests

**Files:**
- Create: `tests/hardware/test_sniff_spi_hardware.py`
- Create: `tests/hardware/test_sniff_uart_hardware.py`
- Create: `tests/hardware/test_sniff_i2c_hardware.py`
- Create: `tests/hardware/test_sniff_can_hardware.py`

- [ ] **Step 12.1: Create UART/I2C/CAN stubs**

`tests/hardware/test_sniff_uart_hardware.py`:
```python
"""UART sniff hardware test.
Requires external UART transmitter on DIO0 (sniff.uart resets the engine on entry,
making concurrent uart.write impossible from the same session).
External setup: USB-UART adapter TX → DIO0.
"""
import pytest

@pytest.mark.hardware
def test_sniff_uart_loopback() -> None:
    pytest.skip("requires external UART transmitter (USB-UART adapter TX → DIO0)")
```

`tests/hardware/test_sniff_i2c_hardware.py`:
```python
"""I2C sniff hardware test.
Requires external I2C device on SDA=DIO0, SCL=DIO1.
The I2C spy and active master share the same hardware engine on the AD3
and cannot coexist on a single device.
"""
import pytest

@pytest.mark.hardware
def test_sniff_i2c_external_device() -> None:
    pytest.skip("requires external I2C device (SDA=DIO0, SCL=DIO1)")
```

`tests/hardware/test_sniff_can_hardware.py`:
```python
"""CAN sniff hardware test.
Requires external CAN transceiver and CAN node on DIO0 (RX).
"""
import pytest

@pytest.mark.hardware
def test_sniff_can_external_device() -> None:
    pytest.skip("requires external CAN device (RX=DIO0)")
```

- [ ] **Step 12.2: Write SPI hardware test**

`tests/hardware/test_sniff_spi_hardware.py`:

```python
"""SPI sniff hardware test.
(fixtures follow the same module-scope pattern as other hardware tests)

Wiring:
  DIO0 = CLK  (SPI master output)
  DIO1 = MOSI (SPI master output, looped to DIO2 via Jumperless)
  DIO2 = MISO (loopback from DIO1)
  DIO3 = CS   (SPI master output, active-low)

sniff.spi_start uses claim_observe (DigitalIn), which does NOT conflict with
spi.configure (protocol.spi engine). Both can run simultaneously.

Run:
  pytest tests/hardware/test_sniff_spi_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("sniff_spi")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    result = asyncio.run(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.run(app.call_tool("waveforms.close", {}))


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"mosi_miso_loop": ("DIO1", "DIO2")})
def test_sniff_spi_captures_active_transfer(app, tmp_path: Path) -> None:
    """sniff.spi_start + spi.transfer + sniff.spi_stop decodes known data."""

    async def run() -> dict:
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
            "cs_pin": "dio3", "mode": 0, "frequency_hz": 100_000,
        })

        start = await app.call_tool("sniff.spi_start", {
            "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
            "cs_pin": "dio3", "mode": 0, "freq_hz": 100_000,
        })
        sniff_id = start["sniff_id"]

        # Transfer known data while sniff is active
        xfer = await app.call_tool("spi.transfer", {"data": [0xA5, 0x5A]})
        assert xfer["sent"] == [0xA5, 0x5A]

        result = await app.call_tool("sniff.spi_stop", {"sniff_id": sniff_id})
        await app.call_tool("spi.release", {})
        return result

    result = asyncio.run(run())
    assert result["artifact_error"] is None, f"artifact_error: {result['artifact_error']}"
    assert result["artifact_path"] is not None
    assert result["count"] >= 2, f"expected ≥2 decoded words, got {result['count']}"

    import pyarrow.parquet as pq
    table = pq.read_table(result["artifact_path"])
    mosi_bytes = [row.as_py() for row in table.column("mosi")]
    assert bytes([0xA5]) in mosi_bytes, "0xA5 not found in decoded MOSI"
    assert bytes([0x5A]) in mosi_bytes, "0x5A not found in decoded MOSI"

    # MISO should match MOSI (loopback)
    miso_bytes = [row.as_py() for row in table.column("miso")]
    for mo, mi in zip(mosi_bytes, miso_bytes):
        assert mo == mi, f"MOSI/MISO mismatch: {mo!r} != {mi!r}"


@pytest.mark.jumperless(connections={"mosi_miso_loop": ("DIO1", "DIO2")})
def test_sniff_spi_lost_samples_zero(app, tmp_path: Path) -> None:
    """Verify no samples are lost during a short capture."""

    async def run() -> dict:
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
            "cs_pin": "dio3", "mode": 0, "frequency_hz": 100_000,
        })
        start = await app.call_tool("sniff.spi_start", {
            "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
            "cs_pin": "dio3", "mode": 0, "freq_hz": 100_000,
        })
        await app.call_tool("spi.transfer", {"data": [0xFF, 0x00, 0xAA, 0x55]})
        result = await app.call_tool("sniff.spi_stop", {"sniff_id": start["sniff_id"]})
        await app.call_tool("spi.release", {})
        return result

    result = asyncio.run(run())
    assert result["lost_samples"] == 0, f"lost_samples={result['lost_samples']}"
    assert result["count"] >= 4
```

- [ ] **Step 12.3: Run hardware test (requires AD3 + Jumperless connected)**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/hardware/test_sniff_spi_hardware.py -v -m hardware
```
Expected: both tests pass

- [ ] **Step 12.4: Confirm non-hardware suite still clean**

```bash
cd ~/work/dwf-mcp/dwf-mcp && pytest -m 'not hardware' --tb=short -q
```
Expected: all pass

- [ ] **Step 12.5: Commit**

```bash
cd ~/work/dwf-mcp/dwf-mcp
git add tests/hardware/test_sniff_spi_hardware.py \
        tests/hardware/test_sniff_uart_hardware.py \
        tests/hardware/test_sniff_i2c_hardware.py \
        tests/hardware/test_sniff_can_hardware.py
git commit -m "feat: add sniff hardware tests (SPI automated, UART/I2C/CAN stubs)"
```

---

## Final verification

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest -m 'not hardware' --tb=short -q
ruff check src/ tests/
mypy src/
```

All three commands should pass cleanly before this plan is considered complete.
