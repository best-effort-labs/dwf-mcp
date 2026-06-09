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

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        **_unused: Any,
    ) -> list[I2cTransaction]:
        if sample_rate_hz <= 0:
            raise ValueError(
                f"sample_rate_hz must be positive, got {sample_rate_hz}"
            )
        sda = samples[:, pin_map["sda"]].astype(np.int8)
        scl = samples[:, pin_map["scl"]].astype(np.int8)
        # Prepend first sample so diff has same length and edges at i index
        # reflect transition into sample i.
        sda_diff = np.diff(np.concatenate([[sda[0]], sda]))
        scl_diff = np.diff(np.concatenate([[scl[0]], scl]))

        out: list[I2cTransaction] = []
        in_txn = False
        pending: list[int] = []
        current_byte = 0
        bit_count = 0
        addr_byte: int | None = None
        nak_idx: int | None = None
        txn_start_idx = 0
        ninth_bit = False

        for i in range(len(sda)):
            # START condition: SDA falls while SCL is high
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
            # STOP condition: SDA rises while SCL is high
            if in_txn and scl[i] and sda_diff[i] == 1:
                if pending or addr_byte is not None:
                    out.append(_finalize_i2c(
                        addr_byte, pending, nak_idx,
                        timestamp_s=txn_start_idx / sample_rate_hz,
                    ))
                in_txn = False
                continue
            # SCL rising edge inside a transaction: sample SDA
            if in_txn and scl_diff[i] == 1:
                if ninth_bit:
                    # ACK/NAK bit: 0 = ACK, 1 = NAK
                    if sda[i] == 1 and nak_idx is None:
                        # nak_idx encoding for _finalize_i2c:
                        #   0       => NAK on address byte
                        #   N (>=1) => NAK on data byte (N - 1)
                        nak_idx = 0 if addr_byte is None else len(pending) + 1
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
