"""Synthetic I2C samples → I2cDecoder → assert decoded transactions."""
from __future__ import annotations

import numpy as np

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
    sda: list[int] = []
    scl: list[int] = []
    samples_per_bit = int(round(sample_rate_hz / clock_hz))
    half = samples_per_bit // 2

    def hold(s: int, c: int, n: int) -> None:
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
