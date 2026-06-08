"""Software I2C decoder for raw DigitalIn captures."""
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder, I2cTransaction


class I2cDecoder(Decoder):
    protocol_name: ClassVar[str] = "i2c"

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        **_unused: Any,
    ) -> list[I2cTransaction]:
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
    address = addr_byte >> 1
    direction = "read" if (addr_byte & 1) else "write"
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
