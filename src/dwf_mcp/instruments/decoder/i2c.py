"""Software I2C decoder for raw DigitalIn captures."""
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder, I2cTransaction


class I2cDecoder(Decoder):
    """Software I2C decoder for raw DigitalIn captures.

    Handles 7-bit and 10-bit addressing, START/STOP framing, and per-byte
    ACK/NAK sampling. The decoder samples SDA on each SCL rising edge,
    accumulates 8 data bits, then samples the 9th bit as ACK (0) or NAK (1).

    Supports streaming via ``init`` / ``feed`` / ``finalize`` (process chunks
    as they arrive) or one-shot via ``decode(samples)``.

    10-bit addressing detection: when the first byte of a transaction
    matches the reserved pattern ``11110xxR``, the byte's A9/A8 bits plus
    the subsequent byte (A7..A0) form the 10-bit address. ``address_bits``
    in the emitted transaction reflects 7 or 10 accordingly. 10-bit READ
    transactions require a repeated-START sequence (write address bytes,
    repeated START, read directive) — see "Repeated START" below.

    Limitations:
        - Repeated START is not handled — combined transactions (write
          register, repeated START, read data) emit only the second half.
          As a consequence, 10-bit reads cannot be fully decoded since the
          read directive after repeated START carries only A9/A8, not the
          full address. Capture each transaction segment separately if you
          need both halves.
        - Clock stretching is not explicitly modelled but is tolerated since
          decoding is edge-driven rather than timing-driven.
    """

    protocol_name: ClassVar[str] = "i2c"

    def init(  # type: ignore[override]
        self,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        **_unused: Any,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError(
                f"sample_rate_hz must be positive, got {sample_rate_hz}"
            )
        self._sda_col = pin_map["sda"]
        self._scl_col = pin_map["scl"]
        self._sample_rate_hz = sample_rate_hz
        # Edge-detection carries the last sample of each line across chunks.
        # Initial idle is HIGH for both. The very first sample of the stream
        # is compared against these; if a real transition happened before the
        # capture began we lose nothing because edges only matter inside
        # transactions, which are gated on a START condition.
        self._prev_sda: int = 1
        self._prev_scl: int = 1
        # Transaction state.
        self._in_txn = False
        self._pending: list[int] = []
        self._current_byte = 0
        self._bit_count = 0
        self._addr_byte: int | None = None
        self._nak_idx: int | None = None
        self._ninth_bit = False
        self._txn_start_abs_idx = 0  # absolute sample index of the START
        # Position tracking.
        self._consumed_total = 0

    def feed(self, samples: np.ndarray) -> list[I2cTransaction]:  # type: ignore[override]
        sda = samples[:, self._sda_col].astype(np.int8)
        scl = samples[:, self._scl_col].astype(np.int8)
        n = len(sda)
        if n == 0:
            return []
        # Edge into the first sample is relative to the previous chunk's tail.
        sda_diff = np.diff(np.concatenate([[self._prev_sda], sda]))
        scl_diff = np.diff(np.concatenate([[self._prev_scl], scl]))

        out: list[I2cTransaction] = []
        for i in range(n):
            abs_i = self._consumed_total + i
            # START: SDA falls while SCL high.
            if scl[i] and sda_diff[i] == -1:
                self._in_txn = True
                self._pending = []
                self._current_byte = 0
                self._bit_count = 0
                self._addr_byte = None
                self._nak_idx = None
                self._ninth_bit = False
                self._txn_start_abs_idx = abs_i
                continue
            # STOP: SDA rises while SCL high.
            if self._in_txn and scl[i] and sda_diff[i] == 1:
                if self._pending or self._addr_byte is not None:
                    out.append(_finalize_i2c(
                        self._addr_byte, self._pending, self._nak_idx,
                        timestamp_s=self._txn_start_abs_idx / self._sample_rate_hz,
                    ))
                self._in_txn = False
                continue
            # SCL rising edge inside a transaction: sample SDA.
            if self._in_txn and scl_diff[i] == 1:
                if self._ninth_bit:
                    if sda[i] == 1 and self._nak_idx is None:
                        self._nak_idx = (
                            0 if self._addr_byte is None else len(self._pending) + 1
                        )
                    if self._addr_byte is None:
                        self._addr_byte = self._current_byte
                    else:
                        self._pending.append(self._current_byte)
                    self._current_byte = 0
                    self._bit_count = 0
                    self._ninth_bit = False
                else:
                    self._current_byte = (self._current_byte << 1) | int(sda[i])
                    self._bit_count += 1
                    if self._bit_count == 8:
                        self._ninth_bit = True

        # Advance position and remember the last sample for next chunk's diff.
        self._consumed_total += n
        self._prev_sda = int(sda[-1])
        self._prev_scl = int(scl[-1])
        return out

    def finalize(self) -> list[I2cTransaction]:  # type: ignore[override]
        # If we ended mid-transaction (no STOP seen), drop the partial state.
        # Emitting a half-decoded transaction would confuse callers more than
        # silently dropping it.
        self._in_txn = False
        self._pending = []
        return []


def _finalize_i2c(
    addr_byte: int | None,
    data_bytes: list[int],
    nak_idx: int | None,
    timestamp_s: float,
) -> I2cTransaction:
    if addr_byte is None:
        return I2cTransaction(
            timestamp_s=timestamp_s, type="write", address=0, address_bits=7,
            data=b"", nak_at_byte=None, error=True,
            error_detail="incomplete transaction (no address byte)",
        )

    # 10-bit address pattern: first byte is 11110_A9_A8_R/W, second byte
    # is A7..A0. Only the write direction is fully decodable from a single
    # START/STOP frame; 10-bit reads need a repeated START which we don't
    # detect, so the read variant falls through to 7-bit handling (it will
    # have an unhelpful address but at least won't be silently misreported).
    is_10bit_write = (
        (addr_byte & 0xF8) == 0xF0
        and (addr_byte & 1) == 0
        and len(data_bytes) >= 1
    )
    if is_10bit_write:
        a98 = (addr_byte >> 1) & 0x03
        a70 = data_bytes[0]
        return _finalize_10bit_write(
            address=(a98 << 8) | a70,
            payload=data_bytes[1:],
            nak_idx=nak_idx,
            timestamp_s=timestamp_s,
        )

    address = addr_byte >> 1
    direction = "read" if (addr_byte & 1) else "write"

    # On a read transaction, the master NAKs the final byte to signal
    # "no more bytes". That terminal NAK is normal protocol, not an error.
    # A NAK at any earlier byte position on a read means the slave aborted
    # mid-read, which IS an error. On writes, any NAK is an error.
    is_terminal_read_nak = (
        direction == "read"
        and nak_idx is not None
        and nak_idx >= 1
        and nak_idx - 1 == len(data_bytes) - 1
    )

    if is_terminal_read_nak:
        return I2cTransaction(
            timestamp_s=timestamp_s,
            type=direction,
            address=address,
            address_bits=7,
            data=bytes(data_bytes),
            nak_at_byte=nak_idx,
            error=False,
            error_detail=None,
        )

    return I2cTransaction(
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


def _finalize_10bit_write(
    address: int,
    payload: list[int],
    nak_idx: int | None,
    timestamp_s: float,
) -> I2cTransaction:
    """Build the transaction for a 10-bit write. ``nak_idx`` semantics for
    10-bit:
      - 0  → NAK on the high address byte (wire byte 0)
      - 1  → NAK on the low address byte  (wire byte 1)
      - N (>=2) → NAK on data byte N - 2
    """
    if nak_idx is None:
        error_detail: str | None = None
    elif nak_idx == 0:
        error_detail = "nak on address byte (high)"
    elif nak_idx == 1:
        error_detail = "nak on address byte (low)"
    else:
        error_detail = f"nak on data byte {nak_idx - 2}"
    return I2cTransaction(
        timestamp_s=timestamp_s,
        type="write",
        address=address,
        address_bits=10,
        data=bytes(payload),
        nak_at_byte=nak_idx,
        error=nak_idx is not None,
        error_detail=error_detail,
    )
