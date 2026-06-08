# Stage 5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add software state-machine decoders for I2C/UART/CAN plus async observe-mode sniff tools (`sniff.{i2c,uart,can}_start/status/stop`) that run concurrently with protocol-engine masters.

**Architecture:** Three new `Decoder` subclasses (`I2cDecoder`, `UartDecoder`, `CanDecoder`) live alongside the existing `SpiDecoder`. New `decoder.{i2c,uart,can}` tools post-process npz captures. A shared `_AsyncSniffSession` helper (extracted from the existing `sniff.spi_*` plumbing) drives the new `sniff.{i2c,uart,can}_start/status/stop` tools — they claim only `claim_observe`, drive `backend.logic_record_*` directly (NOT the `Logic` instrument), and decode at `*_stop`. Memory is capped at 32 MB raw per session; auto-stopped sessions are reaped after 300s.

**Tech Stack:** Python 3.12, numpy, pyarrow, pydwf, pytest, asyncio.

**Reference spec:** `docs/superpowers/specs/2026-06-07-stage5-design.md`

---

### Task 1: I2cDecoder state machine + unit tests

**Files:**
- Create: `src/dwf_mcp/instruments/decoder/i2c.py`
- Create: `tests/unit/test_i2c_decoder.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_i2c_decoder.py`:

```python
"""Synthetic I2C samples → I2cDecoder → assert decoded transactions."""
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.i2c import I2cDecoder


def _i2c_samples(
    transactions: list[tuple[int, bytes, bool]],  # (addr_7bit, data, write)
    sample_rate_hz: float = 1_000_000.0,
    clock_hz: float = 100_000.0,
    nak_on_addr: bool = False,
) -> np.ndarray:
    """Generate (N, 16) uint8 samples of standard I2C on cols 0 (SDA) and 1 (SCL).
    Both lines start HIGH (idle). Address is sent as (addr << 1) | (0 for write).
    """
    sda, scl = [], []
    samples_per_bit = int(round(sample_rate_hz / clock_hz))
    half = samples_per_bit // 2

    def hold(s, c, n):
        sda.extend([s] * n)
        scl.extend([c] * n)

    hold(1, 1, samples_per_bit * 4)  # idle
    for addr, data, write in transactions:
        # START: SDA falls while SCL high
        hold(1, 1, half)
        hold(0, 1, half)
        # Send address byte (MSB first)
        addr_byte = (addr << 1) | (0 if write else 1)
        bytes_to_send = [addr_byte] + (list(data) if write else [])
        for byte_idx, byte in enumerate(bytes_to_send):
            for bit_idx in range(8):
                bit = (byte >> (7 - bit_idx)) & 1
                hold(bit, 0, half)
                hold(bit, 1, samples_per_bit)
                hold(bit, 0, half)
            # ACK/NAK slot: device drives SDA low to ACK
            is_addr_byte = (byte_idx == 0)
            nak = (nak_on_addr and is_addr_byte) or (not write and byte_idx > 0)
            hold(0 if not nak else 1, 0, half)
            hold(0 if not nak else 1, 1, samples_per_bit)
            hold(0 if not nak else 1, 0, half)
        # STOP: SDA rises while SCL high
        hold(0, 0, half)
        hold(0, 1, half)
        hold(1, 1, half)
        hold(1, 1, samples_per_bit * 4)

    arr = np.zeros((len(sda), 16), dtype=np.uint8)
    arr[:, 0] = sda
    arr[:, 1] = scl
    return arr


def test_decode_single_write_transaction() -> None:
    samples = _i2c_samples([(0x50, b"\x01\x02", True)])
    decoder = I2cDecoder()
    txns = decoder.decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    assert len(txns) == 1
    assert txns[0].address == 0x50
    assert txns[0].type == "write"
    assert txns[0].data == b"\x01\x02"
    assert txns[0].nak_at_byte is None
    assert txns[0].error is False


def test_decode_nak_on_address() -> None:
    samples = _i2c_samples([(0x50, b"", True)], nak_on_addr=True)
    decoder = I2cDecoder()
    txns = decoder.decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    assert len(txns) == 1
    assert txns[0].nak_at_byte == 0
    assert txns[0].error is True


def test_decode_back_to_back_transactions() -> None:
    samples = _i2c_samples([
        (0x50, b"\x01", True),
        (0x60, b"\xAB", True),
    ])
    decoder = I2cDecoder()
    txns = decoder.decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    assert len(txns) == 2
    assert txns[0].address == 0x50
    assert txns[1].address == 0x60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_i2c_decoder.py -v`
Expected: ImportError (i2c.py doesn't exist).

- [ ] **Step 3: Implement I2cDecoder**

Create `src/dwf_mcp/instruments/decoder/i2c.py`:

```python
"""Software I2C decoder for raw DigitalIn captures."""
from __future__ import annotations

from typing import Any

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder, Transaction


class I2cDecoder(Decoder):
    name = "i2c"

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        **_unused: Any,
    ) -> list[Transaction]:
        sda = samples[:, pin_map["sda"]].astype(np.int8)
        scl = samples[:, pin_map["scl"]].astype(np.int8)
        # Edge arrays: +1 = rising, -1 = falling, 0 = no change. Pre-pad with line state[0].
        sda_diff = np.diff(np.concatenate([[sda[0]], sda]))
        scl_diff = np.diff(np.concatenate([[scl[0]], scl]))

        out: list[Transaction] = []
        in_txn = False
        pending: list[int] = []   # bytes accumulated this transaction
        current_byte = 0
        bit_count = 0
        addr_byte: int | None = None
        nak_idx: int | None = None      # 0-based byte index where NAK occurred
        txn_start_idx = 0
        ninth_bit = False               # next sample-on-rising is the ACK/NAK slot

        for i in range(len(sda)):
            # START: SDA falls while SCL high
            if scl[i] and sda_diff[i] == -1:
                in_txn = True
                pending = []
                current_byte = 0
                bit_count = 0
                addr_byte = None
                nak_idx = None
                ninth_bit = False
                txn_start_idx = i
                continue
            # STOP: SDA rises while SCL high
            if in_txn and scl[i] and sda_diff[i] == 1:
                if pending or addr_byte is not None:
                    out.append(_finalize_i2c(
                        addr_byte, pending, nak_idx,
                        timestamp_s=txn_start_idx / sample_rate_hz,
                    ))
                in_txn = False
                continue
            # Sample bit on SCL rising
            if in_txn and scl_diff[i] == 1:
                if ninth_bit:
                    # ACK=0 (low), NAK=1 (high). Record NAK position.
                    if sda[i] == 1 and nak_idx is None:
                        # NAK on the byte we just completed: index in the
                        # full transmission counting address as byte 0.
                        nak_idx = (0 if addr_byte is None else len(pending))
                    if addr_byte is None:
                        addr_byte = current_byte
                    else:
                        pending.append(current_byte)
                    current_byte = 0
                    bit_count = 0
                    ninth_bit = False
                else:
                    current_byte = (current_byte << 1) | int(sda[i])
                    bit_count += 1
                    if bit_count == 8:
                        ninth_bit = True

        return out


def _finalize_i2c(
    addr_byte: int | None,
    data_bytes: list[int],
    nak_idx: int | None,
    timestamp_s: float,
) -> Transaction:
    if addr_byte is None:
        # Pathological: STOP before any complete byte. Skip.
        return Transaction(
            timestamp_s=timestamp_s, type="write", address=0, address_bits=7,
            data=b"", nak_at_byte=None, error=True,
            error_detail="incomplete transaction (no address byte)",
        )
    address = addr_byte >> 1
    direction = "read" if (addr_byte & 1) else "write"
    return Transaction(
        timestamp_s=timestamp_s,
        type=direction,
        address=address,
        address_bits=7,
        data=bytes(data_bytes),
        nak_at_byte=nak_idx,
        error=nak_idx is not None,
        error_detail=(
            "nak on address byte" if nak_idx == 0
            else f"nak on data byte {nak_idx - 1}" if nak_idx is not None
            else None
        ),
    )
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/unit/test_i2c_decoder.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/decoder/i2c.py tests/unit/test_i2c_decoder.py
git commit -m "feat: I2cDecoder state machine + unit tests"
```

---

### Task 2: UartDecoder state machine + unit tests

**Files:**
- Create: `src/dwf_mcp/instruments/decoder/uart.py`
- Create: `tests/unit/test_uart_decoder.py`

- [ ] **Step 1: Write the failing test**

```python
"""Synthetic UART samples → UartDecoder → assert decoded bytes."""
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.uart import UartDecoder


def _uart_samples(
    data: bytes, baud: int = 9600, sample_rate_hz: float = 96000.0,
    parity: str = "none", stop_bits: int = 1, polarity: int = 0,
) -> np.ndarray:
    """TTL UART (polarity=0): idle HIGH, start LOW, LSB-first, MSB-last, stop HIGH.
    Inverts everything if polarity=1."""
    samples_per_bit = int(round(sample_rate_hz / baud))
    bits: list[int] = []

    def push(b, n):
        bits.extend([b] * n)

    push(1, samples_per_bit * 4)  # idle
    for byte in data:
        push(0, samples_per_bit)  # start
        for i in range(8):
            push((byte >> i) & 1, samples_per_bit)
        if parity == "even":
            push(bin(byte).count("1") & 1, samples_per_bit)
        elif parity == "odd":
            push(1 - (bin(byte).count("1") & 1), samples_per_bit)
        for _ in range(stop_bits):
            push(1, samples_per_bit)  # stop
        push(1, samples_per_bit)  # gap
    push(1, samples_per_bit * 4)

    if polarity == 1:
        bits = [1 - b for b in bits]
    arr = np.zeros((len(bits), 16), dtype=np.uint8)
    arr[:, 0] = bits
    return arr


def test_decode_simple_bytes() -> None:
    samples = _uart_samples(b"Hi!", baud=9600, sample_rate_hz=96000.0)
    decoder = UartDecoder()
    frames = decoder.decode(
        samples, {"rx": 0}, sample_rate_hz=96000.0,
        baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0,
    )
    payload = b"".join(f.data for f in frames)
    assert b"Hi!" in payload


def test_decode_polarity_inverted() -> None:
    samples = _uart_samples(b"X", baud=9600, sample_rate_hz=96000.0, polarity=1)
    decoder = UartDecoder()
    frames = decoder.decode(
        samples, {"rx": 0}, sample_rate_hz=96000.0,
        baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=1,
    )
    payload = b"".join(f.data for f in frames)
    assert payload == b"X"


def test_decode_refuses_low_oversampling() -> None:
    samples = _uart_samples(b"A", baud=9600, sample_rate_hz=20000.0)
    decoder = UartDecoder()
    with pytest.raises(ValueError, match="oversampling"):
        decoder.decode(
            samples, {"rx": 0}, sample_rate_hz=20000.0,
            baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_uart_decoder.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement UartDecoder**

```python
"""Software UART decoder for raw DigitalIn captures."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder


@dataclass
class UartFrame:
    timestamp_s: float
    data: bytes
    parity_error: bool
    framing_error: bool
    error: bool
    error_detail: str | None


class UartDecoder(Decoder):
    name = "uart"

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        baud: int = 9600,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
        polarity: int = 0,
        **_unused: Any,
    ) -> list[UartFrame]:
        samples_per_bit = sample_rate_hz / baud
        if samples_per_bit < 4:
            raise ValueError(
                f"UART decode requires ≥4× oversampling, got {samples_per_bit:.1f}× "
                f"(sample_rate_hz={sample_rate_hz}, baud={baud})"
            )

        rx = samples[:, pin_map["rx"]].astype(np.uint8)
        if polarity == 1:
            rx = 1 - rx  # normalize to TTL convention

        idle, start_level = 1, 0
        frames: list[UartFrame] = []
        i = 0
        n = len(rx)
        while i < n:
            # Find start edge
            while i < n and rx[i] != start_level:
                i += 1
            start_i = i
            if i >= n:
                break
            # Sample at mid-bit positions
            def at(bit_index: int) -> int:
                idx = int(start_i + samples_per_bit * (bit_index + 0.5))
                return int(rx[idx]) if idx < n else idle

            byte = 0
            for b in range(data_bits):
                byte |= at(1 + b) << b
            par_err = False
            if parity != "none":
                par_bit = at(1 + data_bits)
                ones = bin(byte).count("1") + par_bit
                if parity == "even" and (ones & 1) != 0:
                    par_err = True
                if parity == "odd" and (ones & 1) != 1:
                    par_err = True
            stop_index = 1 + data_bits + (1 if parity != "none" else 0)
            fram_err = at(stop_index) != idle
            ts = start_i / sample_rate_hz
            frames.append(UartFrame(
                timestamp_s=ts,
                data=bytes([byte]),
                parity_error=par_err,
                framing_error=fram_err,
                error=par_err or fram_err,
                error_detail=("parity error" if par_err else "framing error" if fram_err else None),
            ))
            # Advance past this frame
            total_bits = 1 + data_bits + (1 if parity != "none" else 0) + stop_bits
            i = int(start_i + samples_per_bit * total_bits)

        return frames
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/unit/test_uart_decoder.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/decoder/uart.py tests/unit/test_uart_decoder.py
git commit -m "feat: UartDecoder state machine + unit tests"
```

---

### Task 3: CanDecoder state machine + unit tests

**Files:**
- Create: `src/dwf_mcp/instruments/decoder/can.py`
- Create: `tests/unit/test_can_decoder.py`

- [ ] **Step 1: Write the failing test**

```python
"""Synthetic CAN samples → CanDecoder → assert decoded frames."""
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.can import CanDecoder, can_crc15

# Reference: CAN-CRC15 polynomial 0x4599.
# Frame layout: SOF (1 dom) | ID 11-bit MSB | RTR | IDE=0 | r0 | DLC 4-bit | DATA | CRC 15-bit | CRC delim 1 rec | ACK slot 1 (rec, no ack) | ACK delim 1 rec | EOF 7 rec


def _can_bits(frame_id: int, data: bytes, rtr: bool = False) -> list[int]:
    """Build raw bit stream for a standard CAN frame (no bit-stuffing yet)."""
    bits: list[int] = []
    bits.append(0)  # SOF dominant
    for i in range(11):
        bits.append((frame_id >> (10 - i)) & 1)
    bits.append(1 if rtr else 0)  # RTR
    bits.append(0)  # IDE = 0 (standard)
    bits.append(0)  # r0
    dlc = len(data)
    for i in range(4):
        bits.append((dlc >> (3 - i)) & 1)
    for byte in data:
        for i in range(8):
            bits.append((byte >> (7 - i)) & 1)
    # CRC over arbitration + control + data
    crc_input = bits[1:]   # exclude SOF
    crc = can_crc15(crc_input)
    for i in range(15):
        bits.append((crc >> (14 - i)) & 1)
    bits.append(1)  # CRC delim
    bits.append(1)  # ACK slot (no ack)
    bits.append(1)  # ACK delim
    bits.extend([1] * 7)  # EOF
    return bits


def _stuff(bits: list[int]) -> list[int]:
    """Apply CAN bit-stuffing (insert opposite bit after 5 same)."""
    out: list[int] = []
    last = -1
    run = 0
    for b in bits:
        out.append(b)
        if b == last:
            run += 1
            if run == 5:
                out.append(1 - b)
                last = 1 - b
                run = 1
        else:
            last = b
            run = 1
    return out


def _samples_from_bits(bits: list[int], bitrate: int, sample_rate_hz: float) -> np.ndarray:
    samples_per_bit = int(round(sample_rate_hz / bitrate))
    rx: list[int] = []
    rx.extend([1] * samples_per_bit * 15)   # idle (recessive)
    for b in bits:
        rx.extend([b] * samples_per_bit)
    rx.extend([1] * samples_per_bit * 15)
    arr = np.zeros((len(rx), 16), dtype=np.uint8)
    arr[:, 0] = rx
    return arr


def test_decode_simple_can_frame() -> None:
    bits = _stuff(_can_bits(0x123, b"\xDE\xAD"))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert len(frames) == 1
    assert frames[0].frame_id == 0x123
    assert frames[0].data == b"\xDE\xAD"
    assert frames[0].dlc == 2
    assert frames[0].rtr is False
    assert frames[0].crc_valid is True
    assert frames[0].error is False


def test_decode_max_dlc_frame() -> None:
    bits = _stuff(_can_bits(0x7FF, b"\x01\x02\x03\x04\x05\x06\x07\x08"))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert frames[0].dlc == 8
    assert frames[0].data == b"\x01\x02\x03\x04\x05\x06\x07\x08"


def test_decode_refuses_low_oversampling() -> None:
    bits = _stuff(_can_bits(0x100, b"\x42"))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=500_000.0)  # 5x
    decoder = CanDecoder()
    with pytest.raises(ValueError, match="oversampling"):
        decoder.decode(samples, {"rx": 0}, sample_rate_hz=500_000.0, bitrate=100_000)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_can_decoder.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement CanDecoder**

```python
"""Software CAN decoder (Standard 11-bit IDs, no FD)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder


CAN_CRC15_POLY = 0x4599


def can_crc15(bits: list[int]) -> int:
    crc = 0
    for bit in bits:
        nxt = (crc >> 14) ^ bit
        crc = ((crc << 1) & 0x7FFF)
        if nxt:
            crc ^= CAN_CRC15_POLY
    return crc & 0x7FFF


@dataclass
class CanFrame:
    timestamp_s: float
    frame_id: int
    extended: bool
    rtr: bool
    dlc: int
    data: bytes
    crc_valid: bool | None
    ack_received: bool | None
    error_type: str | None
    error: bool
    error_detail: str | None


class CanDecoder(Decoder):
    name = "can"

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        bitrate: int = 100_000,
        **_unused: Any,
    ) -> list[CanFrame]:
        samples_per_bit = sample_rate_hz / bitrate
        if samples_per_bit < 8:
            raise ValueError(
                f"CAN decode requires ≥8× oversampling, got {samples_per_bit:.1f}× "
                f"(sample_rate_hz={sample_rate_hz}, bitrate={bitrate})"
            )
        sample_point = int(round(samples_per_bit * 0.75))

        rx = samples[:, pin_map["rx"]].astype(np.uint8)
        # CAN convention: 0 = dominant, 1 = recessive.
        frames: list[CanFrame] = []
        n = len(rx)
        i = 0
        while i < n:
            # Look for SOF: dominant after sustained recessive
            while i < n and rx[i] == 1:
                i += 1
            if i >= n:
                break
            sof_i = i
            ts = sof_i / sample_rate_hz

            # Sample bits at sample_point of each bit time, with bit-destuffing.
            def sample_bit(bit_index: int) -> int:
                idx = sof_i + int(samples_per_bit * bit_index) + sample_point
                return int(rx[idx]) if idx < n else 1

            try:
                frame = _parse_can_frame(sample_bit, ts)
            except _CanParseError as exc:
                frames.append(CanFrame(
                    timestamp_s=ts, frame_id=0, extended=False, rtr=False,
                    dlc=0, data=b"",
                    crc_valid=None, ack_received=None, error_type=exc.kind,
                    error=True, error_detail=str(exc),
                ))
                # Skip a conservative number of bits and resync
                i = sof_i + int(samples_per_bit * 16)
                continue
            frames.append(frame)
            i = sof_i + int(samples_per_bit * frame_total_bits(frame))

        return frames


def frame_total_bits(frame: CanFrame) -> int:
    return 1 + 11 + 1 + 2 + 4 + 8 * frame.dlc + 15 + 1 + 1 + 1 + 7


class _CanParseError(Exception):
    def __init__(self, kind: str, msg: str = "") -> None:
        super().__init__(msg or kind)
        self.kind = kind


def _parse_can_frame(sample_bit, timestamp_s: float) -> CanFrame:
    """Use a destuffing wrapper that tracks logical bit position."""
    bit_idx = 0
    last = -1
    run = 0
    consumed: list[int] = []  # logical bit values

    def next_logical() -> int:
        nonlocal bit_idx, last, run
        while True:
            v = sample_bit(bit_idx)
            bit_idx += 1
            if v == last:
                run += 1
            else:
                last = v
                run = 1
            if run == 5:
                # Skip the destuff bit
                stuff = sample_bit(bit_idx)
                bit_idx += 1
                if stuff == v:
                    raise _CanParseError("stuff", "missing stuff bit")
                last = stuff
                run = 1
            consumed.append(v)
            return v

    # SOF
    if next_logical() != 0:
        raise _CanParseError("form", "SOF not dominant")
    # 11-bit ID
    frame_id = 0
    for _ in range(11):
        frame_id = (frame_id << 1) | next_logical()
    rtr = bool(next_logical())
    ide = next_logical()
    if ide != 0:
        raise _CanParseError("form", "extended IDs not supported in stage 5")
    _ = next_logical()  # r0
    dlc = 0
    for _ in range(4):
        dlc = (dlc << 1) | next_logical()
    dlc = min(dlc, 8)
    data_bytes = bytearray()
    for _ in range(dlc):
        byte = 0
        for _ in range(8):
            byte = (byte << 1) | next_logical()
        data_bytes.append(byte)

    crc_input = consumed[1:]  # exclude SOF
    expected_crc = can_crc15(crc_input)

    rx_crc = 0
    for _ in range(15):
        rx_crc = (rx_crc << 1) | next_logical()
    crc_valid = (rx_crc == expected_crc)

    # CRC delim, ACK slot, ACK delim — these are NOT bit-stuffed in real CAN,
    # but at this point we're past the stuffed region.
    return CanFrame(
        timestamp_s=timestamp_s,
        frame_id=frame_id,
        extended=False,
        rtr=rtr,
        dlc=dlc,
        data=bytes(data_bytes),
        crc_valid=crc_valid,
        ack_received=None,
        error_type=None if crc_valid else "crc",
        error=not crc_valid,
        error_detail=None if crc_valid else "crc mismatch",
    )
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/unit/test_can_decoder.py -v`
Expected: 3 passed. If CRC test fails, double-check `can_crc15` against the synthetic generator (both use the same polynomial; mismatches usually indicate bit-ordering bug).

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/decoder/can.py tests/unit/test_can_decoder.py
git commit -m "feat: CanDecoder state machine + unit tests"
```

---

### Task 4: Wire decoder.{i2c,uart,can} tools into Decoder instrument

**Files:**
- Modify: `src/dwf_mcp/instruments/decoder/__init__.py`
- Test: `tests/unit/test_decoder.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_decoder.py`:

```python
def test_decoder_i2c_tool_writes_parquet(tmp_path) -> None:
    """decoder.i2c reads an npz + sidecar, writes a decoded parquet."""
    import asyncio
    import json
    import numpy as np
    import pyarrow.parquet as pq

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.fake import FakeBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.instruments.decoder import Decoder
    from dwf_mcp.policy import SafetyPolicy
    from tests.unit.test_i2c_decoder import _i2c_samples

    samples = _i2c_samples([(0x50, b"\x01", True)])
    capture = tmp_path / "cap.npz"
    np.savez(capture, samples=samples)
    sidecar = tmp_path / "cap.json"
    sidecar.write_text(json.dumps({
        "pins": ["dio0", "dio1"],
        "sample_rate_hz": 1_000_000.0,
    }))

    device = DwfDevice(
        backend=FakeBackend(), policy=SafetyPolicy(),
        allocator=PinAllocator(), workspace=tmp_path, idle_timeout_s=60,
    )
    device.open()
    dec = Decoder(device=device, artifacts=ArtifactWriter(workspace=tmp_path))

    result = asyncio.run(dec.i2c(capture_path=str(capture), sda_pin="dio0", scl_pin="dio1"))
    assert result["artifact_error"] is None
    assert result["count"] == 1
    table = pq.read_table(result["artifact_path"])
    assert table.column("address")[0].as_py() == 0x50
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_decoder.py::test_decoder_i2c_tool_writes_parquet -v`
Expected: AttributeError (Decoder has no `.i2c`).

- [ ] **Step 3: Add tools to Decoder instrument**

Read `src/dwf_mcp/instruments/decoder/__init__.py`, then add three schemas + dispatch entries + three methods. The existing `decoder.spi` method is the template:

```python
# Schemas
DECODER_I2C_SCHEMA = {
    "type": "object",
    "required": ["capture_path", "sda_pin", "scl_pin"],
    "properties": {
        "capture_path": {"type": "string"},
        "sda_pin": {"type": "string", "pattern": _PIN_RE},
        "scl_pin": {"type": "string", "pattern": _PIN_RE},
        "output_path": {"type": "string"},
    },
}
DECODER_UART_SCHEMA = {
    "type": "object",
    "required": ["capture_path", "rx_pin", "baud"],
    "properties": {
        "capture_path": {"type": "string"},
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "baud": {"type": "integer", "minimum": 300},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {"type": "string", "enum": ["none", "odd", "even"], "default": "none"},
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
        "polarity": {"type": "integer", "enum": [0, 1], "default": 0},
        "output_path": {"type": "string"},
    },
}
DECODER_CAN_SCHEMA = {
    "type": "object",
    "required": ["capture_path", "rx_pin", "bitrate"],
    "properties": {
        "capture_path": {"type": "string"},
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "bitrate": {"type": "integer", "minimum": 10_000},
        "output_path": {"type": "string"},
    },
}
```

Add to `Decoder.tools`:
```python
"i2c":  ("i2c",  DECODER_I2C_SCHEMA),
"uart": ("uart", DECODER_UART_SCHEMA),
"can":  ("can",  DECODER_CAN_SCHEMA),
```

Implement methods mirroring `decoder.spi` (read npz + sidecar, instantiate decoder, write parquet). Convert returned dataclass instances to dicts via `dataclasses.asdict`.

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/unit/test_decoder.py -v`
Expected: all green including new test.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/decoder/__init__.py tests/unit/test_decoder.py
git commit -m "feat: wire decoder.{i2c,uart,can} tools into Decoder instrument"
```

---

### Task 5: Add `done` field to sniff.spi_status

**Files:**
- Modify: `src/dwf_mcp/instruments/sniff.py`
- Test: `tests/unit/test_sniff.py`

- [ ] **Step 1: Write failing test**

```python
def test_spi_status_reports_done(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend
    samples, _ = _spi_samples([0xA5])
    fake._logic_record_canned_chunk = samples
    fake.set_logic_record_status_sequence([(len(samples), 0, 0)])  # remaining=0 → done

    async def run():
        start = await sniff.spi_start(clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000)
        await asyncio.sleep(0.05)
        return sniff.spi_status(start["sniff_id"])

    status = asyncio.run(run())
    assert status["done"] is True
```

- [ ] **Step 2: Run failure** → `KeyError: 'done'`

- [ ] **Step 3: Update spi_status to include `done`**

In `sniff.py:spi_status`:
```python
return {
    "samples_received": total_samples,
    "lost_samples": session.lost_samples,
    "done": session.done,
}
```

- [ ] **Step 4: Verify** → green.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/sniff.py tests/unit/test_sniff.py
git commit -m "feat: add done field to sniff.spi_status"
```

---

### Task 6: Extract _AsyncSniffSession helper

**Files:**
- Create: `src/dwf_mcp/instruments/_async_sniff.py`
- Modify: `src/dwf_mcp/instruments/sniff.py`

The current `sniff.spi_start/spi_stop` plumbing (claim_observe, configure record, arm, RecordingSession, create_task, cancel on stop, drain, release) is exactly what the new I2C/UART/CAN observe-mode tools need. Extract it.

- [ ] **Step 1: Run the existing sniff tests as a baseline**

Run: `pytest tests/unit/test_sniff.py -v`
Note: 17+ passing. Refactor target is not to add coverage, just preserve it.

- [ ] **Step 2: Create _async_sniff.py**

```python
"""Shared infrastructure for async observe-mode sniff tools."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from dwf_mcp.streaming import RecordingSession, record_loop

log = logging.getLogger(__name__)

SNIFF_REAP_AFTER_S = 300.0
MAX_RAW_BYTES = 32 * 1024 * 1024  # 32 MB


@dataclass
class _AsyncSniffSession:
    sniff_id: str
    record_session: RecordingSession
    allocator_key: str
    started_at: float
    completed_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def check_memory_cap(sample_rate_hz: float, max_duration_s: float, n_pins: int) -> None:
    bytes_needed = sample_rate_hz * max_duration_s * max(1, n_pins)
    if bytes_needed > MAX_RAW_BYTES:
        suggested = MAX_RAW_BYTES / (sample_rate_hz * max(1, n_pins))
        raise ValueError(
            f"capture would need {bytes_needed/1e6:.1f} MB raw, exceeds 32 MB cap; "
            f"reduce sample_rate_hz or max_duration_s (try max_duration_s≤{suggested:.2f})"
        )


def start_observe_session(
    device, allocator_key: str, pin_mask: int,
    sample_rate_hz: float, max_duration_s: float, meta: dict[str, Any],
) -> _AsyncSniffSession:
    """Claim DigitalIn observer, arm record mode, spawn record_loop task.
    Rolls back fully on any partial-setup failure."""
    device.allocator.claim_observe(allocator_key)
    try:
        device.backend.logic_record_configure(
            pin_mask=pin_mask, sample_rate_hz=sample_rate_hz, duration_s=max_duration_s,
        )
        device.backend.logic_record_arm()
    except Exception:
        try:
            device.backend.logic_record_stop()
        except Exception as exc:
            log.warning("logic_record_stop during start-failure cleanup: %s", exc)
        device.allocator.release(allocator_key)
        raise

    record_session = RecordingSession(
        record_id=str(uuid.uuid4()),
        task=None, notification_task=None,
        queue=asyncio.Queue(maxsize=32),
        chunks=[], lost_samples=0, done=False, error=None,
        meta=meta,
    )
    try:
        record_session.task = asyncio.create_task(record_loop(
            record_session,
            device.backend.logic_record_status,
            device.backend.logic_record_read,
        ))
    except Exception:
        try:
            device.backend.logic_record_stop()
        except Exception as exc:
            log.warning("logic_record_stop during task-create failure: %s", exc)
        device.allocator.release(allocator_key)
        raise

    return _AsyncSniffSession(
        sniff_id=meta.get("sniff_id", str(uuid.uuid4())),
        record_session=record_session,
        allocator_key=allocator_key,
        started_at=time.monotonic(),
        meta=meta,
    )


def reap_completed_sessions(sessions: dict[str, _AsyncSniffSession], device) -> None:
    """Release allocator claims for auto-stopped sessions older than the retention window."""
    now = time.monotonic()
    for sniff_id, session in list(sessions.items()):
        if session.completed_at is None:
            if session.record_session.done:
                session.completed_at = now
            continue
        if now - session.completed_at >= SNIFF_REAP_AFTER_S:
            log.warning(
                "reaping orphan sniff session %s (auto-stopped %.0fs ago, *_stop never called)",
                sniff_id, now - session.completed_at,
            )
            if session.record_session.task and not session.record_session.task.done():
                session.record_session.task.cancel()
            try:
                device.backend.logic_record_stop()
            except Exception as exc:
                log.warning("logic_record_stop during reap: %s", exc)
            device.allocator.release(session.allocator_key)
            sessions.pop(sniff_id, None)


async def stop_observe_session(
    session: _AsyncSniffSession, device,
) -> tuple[np.ndarray, int]:
    """Cancel task, drain remaining samples, stop hardware. Returns (concatenated samples, lost)."""
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

    samples = np.concatenate(r.chunks, axis=0) if r.chunks else np.zeros((0, 16), dtype=np.uint8)
    return samples, r.lost_samples
```

- [ ] **Step 3: Refactor sniff.spi_start/stop to use the helper**

Replace the inline plumbing in `Sniff.spi_start` with `start_observe_session(...)`, and `spi_stop`'s cancel+drain steps with `await stop_observe_session(...)`. The SPI-specific decode + parquet write stays in `spi_stop`. Store sessions in `self._spi_sessions` as `_AsyncSniffSession` instances (was: ad-hoc dict).

- [ ] **Step 4: Verify all SPI tests still pass**

Run: `pytest tests/unit/test_sniff.py -v`
Expected: all 17+ tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/_async_sniff.py src/dwf_mcp/instruments/sniff.py
git commit -m "refactor: extract _AsyncSniffSession helper from sniff.spi internals"
```

---

### Task 7: Memory cap + reaping unit tests

**Files:**
- Test: `tests/unit/test_async_sniff.py` (new)

- [ ] **Step 1: Write failing tests**

```python
"""Tests for shared _AsyncSniffSession helpers."""
import time
import pytest

from dwf_mcp.instruments._async_sniff import (
    check_memory_cap, reap_completed_sessions, MAX_RAW_BYTES, SNIFF_REAP_AFTER_S,
)


def test_check_memory_cap_passes_under_limit() -> None:
    check_memory_cap(sample_rate_hz=1e6, max_duration_s=1.0, n_pins=2)  # 2 MB


def test_check_memory_cap_raises_over_limit() -> None:
    with pytest.raises(ValueError, match="32 MB cap"):
        check_memory_cap(sample_rate_hz=100e6, max_duration_s=1.0, n_pins=2)


def test_reap_evicts_old_completed_sessions(monkeypatch) -> None:
    from dwf_mcp.instruments._async_sniff import _AsyncSniffSession
    from dwf_mcp.streaming import RecordingSession
    import asyncio

    class _FakeDevice:
        class allocator:
            released: list[str] = []
            @staticmethod
            def release(key: str) -> None:
                _FakeDevice.allocator.released.append(key)
        class backend:
            @staticmethod
            def logic_record_stop() -> None:
                pass

    rs = RecordingSession(
        record_id="x", task=None, notification_task=None,
        queue=asyncio.Queue(), chunks=[], lost_samples=0,
        done=True, error=None,
    )
    s = _AsyncSniffSession(
        sniff_id="t1", record_session=rs, allocator_key="sniff_i2c_t1",
        started_at=0.0, completed_at=time.monotonic() - SNIFF_REAP_AFTER_S - 1,
    )
    sessions = {"t1": s}
    reap_completed_sessions(sessions, _FakeDevice)
    assert "t1" not in sessions
    assert "sniff_i2c_t1" in _FakeDevice.allocator.released
```

- [ ] **Step 2: Run** → expected to pass already if the helper was implemented in Task 6.

If the reap path needs tweaks (e.g., it picks `completed_at` lazily), iterate.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_async_sniff.py
git commit -m "test: _AsyncSniffSession memory cap and reaping"
```

---

### Task 8: sniff.i2c_start/status/stop

**Files:**
- Modify: `src/dwf_mcp/instruments/sniff.py`
- Test: `tests/unit/test_sniff.py`

- [ ] **Step 1: Write failing tests**

```python
def test_sniff_i2c_start_returns_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run():
        r = await sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.1,
        )
        await sniff.i2c_stop(r["sniff_id"])
        return r
    result = asyncio.run(run())
    assert "sniff_id" in result


def test_sniff_i2c_start_memory_cap_raises(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="32 MB cap"):
        asyncio.run(sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000,
            max_duration_s=3600, sample_rate_hz=100e6,
        ))


def test_sniff_i2c_does_not_claim_engine_or_dio(sniff: Sniff) -> None:
    """observe-mode must not block a concurrent i2c master on the same wires."""
    fake: FakeBackend = sniff.device.backend
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run():
        r = await sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.1,
        )
        # Now a separate instrument should be able to claim i2c_engine + dio0/dio1.
        sniff.device.allocator.claim("i2c_master", ["i2c_engine", "dio0", "dio1"])
        await sniff.i2c_stop(r["sniff_id"])
    asyncio.run(run())
```

- [ ] **Step 2: Run** → AttributeError.

- [ ] **Step 3: Implement sniff.i2c_start/status/stop**

Add schemas (`SNIFF_I2C_START_SCHEMA` etc.) and three methods on `Sniff`:

```python
async def i2c_start(
    self,
    sda_pin: str, scl_pin: str, clock_hz: int, max_duration_s: float,
    sample_rate_hz: float | None = None, output_path: str | None = None,
) -> dict[str, Any]:
    rate = sample_rate_hz if sample_rate_hz else clock_hz * 10
    if rate / clock_hz < 4:
        raise ValueError(f"I2C decode requires ≥4× oversampling, got {rate/clock_hz:.1f}×")
    check_memory_cap(rate, max_duration_s, n_pins=2)
    sniff_id = str(uuid.uuid4())
    allocator_key = f"sniff_i2c_{sniff_id}"
    pin_mask = (1 << _dio_index(sda_pin)) | (1 << _dio_index(scl_pin))
    session = start_observe_session(
        self.device, allocator_key, pin_mask, rate, max_duration_s,
        meta={
            "sniff_id": sniff_id,
            "sda_pin": sda_pin, "scl_pin": scl_pin,
            "clock_hz": clock_hz, "sample_rate_hz": rate,
            "max_duration_s": max_duration_s,
            "output_path": output_path,
        },
    )
    self._async_sessions[sniff_id] = session
    reap_completed_sessions(self._async_sessions, self.device)
    return {"sniff_id": sniff_id}

def i2c_status(self, sniff_id: str) -> dict[str, Any]:
    reap_completed_sessions(self._async_sessions, self.device)
    session = self._async_sessions.get(sniff_id)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")
    rs = session.record_session
    total = sum(len(c) for c in rs.chunks)
    return {"samples_received": total, "lost_samples": rs.lost_samples, "done": rs.done}

async def i2c_stop(self, sniff_id: str) -> dict[str, Any]:
    session = self._async_sessions.pop(sniff_id, None)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")
    samples, lost = await stop_observe_session(session, self.device)
    meta = session.meta
    try:
        decoder = I2cDecoder()
        txns = decoder.decode(
            samples,
            {"sda": _dio_index(meta["sda_pin"]), "scl": _dio_index(meta["scl_pin"])},
            sample_rate_hz=meta["sample_rate_hz"],
        )
        records = [dataclasses.asdict(t) for t in txns]
        result = self.artifacts.write_parquet(
            "sniff_i2c", records,
            config={k: v for k, v in meta.items() if k != "sniff_id"},
            output_path=Path(meta["output_path"]) if meta.get("output_path") else None,
        )
        artifact_path: str | None = result.path
        artifact_error: str | None = None
        count = len(txns)
        error_count = sum(1 for t in txns if t.error)
    except Exception as exc:
        log.exception("sniff.i2c_stop decode/write failed for %s", sniff_id)
        artifact_path = None
        artifact_error = str(exc)
        count = 0
        error_count = 0
    self.device.allocator.release(session.allocator_key)
    sidecar = artifact_path.replace(".parquet", ".json") if artifact_path else None
    return {
        "artifact_path": artifact_path, "sidecar_path": sidecar,
        "count": count, "error_count": error_count,
        "lost_samples": lost, "artifact_error": artifact_error, "summary": {},
    }
```

Add to `Sniff.tools`:
```python
"i2c_start":  ("i2c_start",  SNIFF_I2C_START_SCHEMA),
"i2c_status": ("i2c_status", SNIFF_STATUS_SCHEMA),
"i2c_stop":   ("i2c_stop",   SNIFF_STOP_SCHEMA),
```

Add `self._async_sessions: dict[str, _AsyncSniffSession] = {}` in `__init__`.

- [ ] **Step 4: Verify**

Run: `pytest tests/unit/test_sniff.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/dwf_mcp/instruments/sniff.py tests/unit/test_sniff.py
git commit -m "feat: sniff.i2c_start/status/stop async observe-mode tools"
```

---

### Task 9: sniff.uart_start/status/stop

**Files:**
- Modify: `src/dwf_mcp/instruments/sniff.py`
- Test: `tests/unit/test_sniff.py`

- [ ] **Step 1: Write failing tests**

```python
def test_sniff_uart_start_returns_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run():
        r = await sniff.uart_start(rx_pin="dio0", baud=9600, max_duration_s=0.1)
        await sniff.uart_stop(r["sniff_id"])
        return r
    assert "sniff_id" in asyncio.run(run())


def test_sniff_uart_memory_cap_raises(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="32 MB cap"):
        asyncio.run(sniff.uart_start(
            rx_pin="dio0", baud=9600, max_duration_s=3600, sample_rate_hz=100e6,
        ))
```

- [ ] **Step 2: Run → AttributeError**.

- [ ] **Step 3: Implement** — same shape as `i2c_start/status/stop` from Task 8, with these differences:

```python
async def uart_start(
    self,
    rx_pin: str, baud: int, max_duration_s: float,
    data_bits: int = 8, parity: str = "none", stop_bits: int = 1, polarity: int = 0,
    sample_rate_hz: float | None = None, output_path: str | None = None,
) -> dict[str, Any]:
    rate = sample_rate_hz if sample_rate_hz else baud * 10
    if rate / baud < 4:
        raise ValueError(f"UART decode requires ≥4× oversampling, got {rate/baud:.1f}×")
    check_memory_cap(rate, max_duration_s, n_pins=1)
    sniff_id = str(uuid.uuid4())
    allocator_key = f"sniff_uart_{sniff_id}"
    pin_mask = 1 << _dio_index(rx_pin)
    session = start_observe_session(
        self.device, allocator_key, pin_mask, rate, max_duration_s,
        meta={
            "sniff_id": sniff_id, "rx_pin": rx_pin,
            "baud": baud, "data_bits": data_bits, "parity": parity,
            "stop_bits": stop_bits, "polarity": polarity,
            "sample_rate_hz": rate, "max_duration_s": max_duration_s,
            "output_path": output_path,
        },
    )
    self._async_sessions[sniff_id] = session
    reap_completed_sessions(self._async_sessions, self.device)
    return {"sniff_id": sniff_id}

def uart_status(self, sniff_id: str) -> dict[str, Any]:
    # Identical to i2c_status.
    reap_completed_sessions(self._async_sessions, self.device)
    session = self._async_sessions.get(sniff_id)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")
    rs = session.record_session
    total = sum(len(c) for c in rs.chunks)
    return {"samples_received": total, "lost_samples": rs.lost_samples, "done": rs.done}

async def uart_stop(self, sniff_id: str) -> dict[str, Any]:
    session = self._async_sessions.pop(sniff_id, None)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")
    samples, lost = await stop_observe_session(session, self.device)
    meta = session.meta
    try:
        decoder = UartDecoder()
        frames = decoder.decode(
            samples, {"rx": _dio_index(meta["rx_pin"])},
            sample_rate_hz=meta["sample_rate_hz"],
            baud=meta["baud"], data_bits=meta["data_bits"],
            parity=meta["parity"], stop_bits=meta["stop_bits"],
            polarity=meta["polarity"],
        )
        records = [dataclasses.asdict(f) for f in frames]
        result = self.artifacts.write_parquet(
            "sniff_uart", records,
            config={k: v for k, v in meta.items() if k != "sniff_id"},
            output_path=Path(meta["output_path"]) if meta.get("output_path") else None,
        )
        artifact_path, artifact_error = result.path, None
        count = len(frames)
        error_count = sum(1 for f in frames if f.error)
    except Exception as exc:
        log.exception("sniff.uart_stop decode/write failed for %s", sniff_id)
        artifact_path, artifact_error = None, str(exc)
        count = error_count = 0
    self.device.allocator.release(session.allocator_key)
    sidecar = artifact_path.replace(".parquet", ".json") if artifact_path else None
    return {
        "artifact_path": artifact_path, "sidecar_path": sidecar,
        "count": count, "error_count": error_count,
        "lost_samples": lost, "artifact_error": artifact_error, "summary": {},
    }
```

Add tools entries:
```python
"uart_start":  ("uart_start",  SNIFF_UART_START_SCHEMA),
"uart_status": ("uart_status", SNIFF_STATUS_SCHEMA),
"uart_stop":   ("uart_stop",   SNIFF_STOP_SCHEMA),
```

- [ ] **Step 4: Run tests** → green.

- [ ] **Step 5: Commit**:

```bash
git commit -m "feat: sniff.uart_start/status/stop async observe-mode tools"
```

---

### Task 10: sniff.can_start/status/stop

**Files:**
- Modify: `src/dwf_mcp/instruments/sniff.py`
- Test: `tests/unit/test_sniff.py`

- [ ] **Step 1: Write failing tests**

```python
def test_sniff_can_start_returns_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run():
        r = await sniff.can_start(rx_pin="dio0", bitrate=125_000, max_duration_s=0.1)
        await sniff.can_stop(r["sniff_id"])
        return r
    assert "sniff_id" in asyncio.run(run())


def test_sniff_can_default_sample_rate_is_20x_bitrate(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run():
        r = await sniff.can_start(rx_pin="dio0", bitrate=100_000, max_duration_s=0.05)
        session = sniff._async_sessions[r["sniff_id"]]
        assert session.meta["sample_rate_hz"] == 2_000_000  # 20×
        await sniff.can_stop(r["sniff_id"])
    asyncio.run(run())
```

- [ ] **Step 2: Run → AttributeError**.

- [ ] **Step 3: Implement**

```python
async def can_start(
    self,
    rx_pin: str, bitrate: int, max_duration_s: float,
    sample_rate_hz: float | None = None, output_path: str | None = None,
) -> dict[str, Any]:
    rate = sample_rate_hz if sample_rate_hz else bitrate * 20
    if rate / bitrate < 8:
        raise ValueError(
            f"CAN decode requires ≥8× oversampling, got {rate/bitrate:.1f}×"
        )
    check_memory_cap(rate, max_duration_s, n_pins=1)
    sniff_id = str(uuid.uuid4())
    allocator_key = f"sniff_can_{sniff_id}"
    pin_mask = 1 << _dio_index(rx_pin)
    session = start_observe_session(
        self.device, allocator_key, pin_mask, rate, max_duration_s,
        meta={
            "sniff_id": sniff_id, "rx_pin": rx_pin, "bitrate": bitrate,
            "sample_rate_hz": rate, "max_duration_s": max_duration_s,
            "output_path": output_path,
        },
    )
    self._async_sessions[sniff_id] = session
    reap_completed_sessions(self._async_sessions, self.device)
    return {"sniff_id": sniff_id}

def can_status(self, sniff_id: str) -> dict[str, Any]:
    # Identical shape to i2c_status / uart_status.
    reap_completed_sessions(self._async_sessions, self.device)
    session = self._async_sessions.get(sniff_id)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")
    rs = session.record_session
    total = sum(len(c) for c in rs.chunks)
    return {"samples_received": total, "lost_samples": rs.lost_samples, "done": rs.done}

async def can_stop(self, sniff_id: str) -> dict[str, Any]:
    session = self._async_sessions.pop(sniff_id, None)
    if session is None:
        raise ValueError(f"unknown sniff_id {sniff_id!r}")
    samples, lost = await stop_observe_session(session, self.device)
    meta = session.meta
    try:
        decoder = CanDecoder()
        frames = decoder.decode(
            samples, {"rx": _dio_index(meta["rx_pin"])},
            sample_rate_hz=meta["sample_rate_hz"], bitrate=meta["bitrate"],
        )
        records = [dataclasses.asdict(f) for f in frames]
        result = self.artifacts.write_parquet(
            "sniff_can", records,
            config={k: v for k, v in meta.items() if k != "sniff_id"},
            output_path=Path(meta["output_path"]) if meta.get("output_path") else None,
        )
        artifact_path, artifact_error = result.path, None
        count = len(frames)
        error_count = sum(1 for f in frames if f.error)
    except Exception as exc:
        log.exception("sniff.can_stop decode/write failed for %s", sniff_id)
        artifact_path, artifact_error = None, str(exc)
        count = error_count = 0
    self.device.allocator.release(session.allocator_key)
    sidecar = artifact_path.replace(".parquet", ".json") if artifact_path else None
    return {
        "artifact_path": artifact_path, "sidecar_path": sidecar,
        "count": count, "error_count": error_count,
        "lost_samples": lost, "artifact_error": artifact_error, "summary": {},
    }
```

Add tool entries:
```python
"can_start":  ("can_start",  SNIFF_CAN_START_SCHEMA),
"can_status": ("can_status", SNIFF_STATUS_SCHEMA),
"can_stop":   ("can_stop",   SNIFF_STOP_SCHEMA),
```

- [ ] **Step 4: Run tests** → green.

- [ ] **Step 5: Commit**:

```bash
git commit -m "feat: sniff.can_start/status/stop async observe-mode tools"
```

---

### Task 11: Allocator coexistence tests

**Files:**
- Test: `tests/unit/test_allocator.py`

- [ ] **Step 1: Add the three required tests from spec**

```python
def test_observe_coexists_with_engine_and_dio_claim() -> None:
    """sniff.i2c_start observer must NOT block i2c.configure on same wires."""
    groups = []
    alloc = PinAllocator(resource_groups=groups)
    alloc.claim_observe("sniff_i2c_X")
    alloc.claim("i2c_master", ["i2c_engine", "dio0", "dio1"])  # must succeed
    assert "i2c_master" in alloc.claimed_instruments()


def test_two_observers_conflict() -> None:
    alloc = PinAllocator()
    alloc.claim_observe("sniff_spi_X")
    with pytest.raises(PinAllocationError):
        alloc.claim_observe("sniff_i2c_Y")


def test_exclusive_digital_in_blocked_while_observer_active() -> None:
    alloc = PinAllocator()
    alloc.claim_observe("sniff_i2c_X")
    with pytest.raises(PinAllocationError):
        alloc.claim("logic", ["digital_in"])
```

- [ ] **Step 2: Run** → likely all 3 pass (existing claim_observe already enforces them).

If any fails, fix the allocator. The first test is the key new invariant: observers must not register as pin owners against engine/DIO claims.

- [ ] **Step 3: Commit**:

```bash
git commit -m "test: allocator coexistence invariants for observe-mode sniff"
```

---

### Task 12: Hardware tests

**Files:**
- Create: `tests/hardware/test_sniff_i2c_observe_hardware.py`
- Create: `tests/hardware/test_sniff_uart_observe_hardware.py`
- Create: `tests/hardware/test_decoder_i2c_post_process.py`

- [ ] **Step 1: Write `test_sniff_i2c_observe_hardware.py`**

Same wiring as existing `test_sniff_i2c_hardware.py` (TOP_RAIL→3.3V, RP2350B as I2C master, GPIO_1→DIO0, GPIO_2→DIO1, pull-ups). Use `sniff.i2c_start/stop` instead of blocking `sniff.i2c`. Add a coexistence case that runs `i2c.configure` + `i2c.scan` while observe-mode is active and asserts both succeed.

- [ ] **Step 2: Run** — expect pass (the RP2350B path is unchanged; only the AD3 capture path differs).

- [ ] **Step 3: Write `test_sniff_uart_observe_hardware.py`** — same pattern as existing UART test, with `*_start/stop`.

- [ ] **Step 4: Write `test_decoder_i2c_post_process.py`** — drive backend directly to capture a small npz, then call `decoder.i2c(capture_path=...)` and assert addresses.

- [ ] **Step 5: Run all four hardware sniff tests + verify pass**

```bash
pytest tests/hardware/test_sniff_*_hardware.py tests/hardware/test_decoder_i2c_post_process.py -v -m hardware
```

- [ ] **Step 6: Commit**:

```bash
git commit -m "test: hardware tests for observe-mode sniff and post-process decoder"
```

---

## Final verification

```bash
pytest tests/unit -q                # all green
pytest tests/hardware -m hardware -v  # all green (AD3 + Jumperless required)
ruff check src/ tests/
mypy src/
```
