"""Synthetic I2C samples → I2cDecoder → assert decoded transactions."""
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.i2c import I2cDecoder


def _i2c_samples(
    transactions: list[tuple[int, bytes, bool]],  # (addr, data, write)
    sample_rate_hz: float = 1_000_000.0,
    clock_hz: float = 100_000.0,
    nak_on_addr: bool = False,
    addr_bits: int = 7,
) -> np.ndarray:
    """Generate (N, 16) uint8 samples of standard I2C on cols 0 (SDA) and 1 (SCL).
    Both lines start HIGH (idle). When ``addr_bits=10``, each transaction's
    address is sent as the 10-bit reserved pattern ``11110_A9_A8_R/W`` followed
    by the low 8 address bits.
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
        # Build the address-byte preamble. For 7-bit: one byte. For 10-bit:
        # two bytes (high = 11110_A9_A8_RW, low = A7..A0).
        if addr_bits == 10:
            a98 = (addr >> 8) & 0x03
            high_addr_byte = 0xF0 | (a98 << 1) | (0 if write else 1)
            low_addr_byte = addr & 0xFF
            bytes_to_send = [high_addr_byte, low_addr_byte] + list(data)
        else:
            addr_byte = (addr << 1) | (0 if write else 1)
            bytes_to_send = [addr_byte] + list(data)
        last_data_idx = len(bytes_to_send) - 1
        for byte_idx, byte in enumerate(bytes_to_send):
            for bit_idx in range(8):
                bit = (byte >> (7 - bit_idx)) & 1
                hold(bit, 0, half)
                hold(bit, 1, samples_per_bit)
                hold(bit, 0, half)
            # ACK/NAK slot.
            #   Address byte(s): slave ACKs (unless nak_on_addr is set,
            #     in which case ONLY the very first address byte NAKs).
            #   Write data bytes: slave ACKs every byte.
            #   Read data bytes: master ACKs every byte except the last,
            #     which it NAKs to signal "no more bytes".
            n_addr_bytes = 2 if addr_bits == 10 else 1
            is_addr_byte = byte_idx < n_addr_bytes
            if is_addr_byte:
                nak = nak_on_addr and byte_idx == 0
            elif write:
                nak = False
            else:
                nak = (byte_idx == last_data_idx)
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


def test_decode_read_transaction() -> None:
    """Read direction: addr LSB = 1, master NAKs the final byte to terminate read."""
    # Build samples for: master reads 2 bytes from 0x50.
    # Address byte = (0x50 << 1) | 1 = 0xA1. Master ACKs after slave sends each data byte
    # EXCEPT the last byte where master NAKs to end the read.
    # The synthetic generator already does this when write=False.
    samples = _i2c_samples([(0x50, b"\xAB\xCD", False)])
    decoder = I2cDecoder()
    txns = decoder.decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    assert len(txns) == 1
    assert txns[0].type == "read"
    assert txns[0].address == 0x50
    # The decoder currently captures the slave-driven data bytes regardless of who NAKed.
    # Terminal NAK on a read is normal and should NOT be flagged as error.
    assert txns[0].data == b"\xAB\xCD"
    assert txns[0].error is False
    assert txns[0].error_detail is None


def test_decode_10bit_write_transaction() -> None:
    """10-bit address write: 11110_A9_A8_0, then A7..A0, then data."""
    addr10 = 0x123  # A9..A0 = 0b01_0010_0011
    samples = _i2c_samples(
        [(addr10, b"\xAB\xCD", True)], addr_bits=10,
    )
    decoder = I2cDecoder()
    txns = decoder.decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    assert len(txns) == 1
    assert txns[0].address == 0x123
    assert txns[0].address_bits == 10
    assert txns[0].type == "write"
    assert txns[0].data == b"\xAB\xCD"
    assert txns[0].error is False


def test_decode_10bit_max_address() -> None:
    """10-bit address 0x3FF (all 10 bits set) decodes correctly."""
    samples = _i2c_samples([(0x3FF, b"\x42", True)], addr_bits=10)
    decoder = I2cDecoder()
    txns = decoder.decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    assert txns[0].address == 0x3FF
    assert txns[0].address_bits == 10
    assert txns[0].data == b"\x42"


def test_decode_10bit_nak_on_high_address_byte() -> None:
    """NAK on the high address byte of a 10-bit write reports as
    nak_at_byte=0 with detail 'nak on address byte (high)'."""
    samples = _i2c_samples(
        [(0x123, b"", True)], addr_bits=10, nak_on_addr=True,
    )
    decoder = I2cDecoder()
    txns = decoder.decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    # With NAK on the high address byte, the master would normally stop —
    # but our generator still emits the rest of the bytes. The decoder
    # records the NAK on the address byte either way.
    assert len(txns) == 1
    assert txns[0].address_bits == 10
    assert txns[0].nak_at_byte == 0
    assert txns[0].error_detail == "nak on address byte (high)"
    assert txns[0].error is True


def test_decode_mixes_7bit_and_10bit_writes() -> None:
    """A 7-bit and a 10-bit write back-to-back decode with the correct
    address_bits set on each."""
    samples_7bit = _i2c_samples([(0x50, b"\x11", True)])
    samples_10bit = _i2c_samples([(0x123, b"\x22", True)], addr_bits=10)
    combined = np.concatenate([samples_7bit, samples_10bit], axis=0)
    decoder = I2cDecoder()
    txns = decoder.decode(combined, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    assert len(txns) == 2
    assert txns[0].address == 0x50 and txns[0].address_bits == 7
    assert txns[1].address == 0x123 and txns[1].address_bits == 10


def test_decode_rejects_zero_sample_rate() -> None:
    decoder = I2cDecoder()
    with pytest.raises(ValueError, match="sample_rate_hz must be positive"):
        decoder.decode(
            np.zeros((10, 16), dtype=np.uint8), {"sda": 0, "scl": 1},
            sample_rate_hz=0,
        )


# --- Streaming API tests --------------------------------------------------

def test_streaming_single_chunk_matches_oneshot() -> None:
    samples = _i2c_samples([(0x50, b"\x01\x02", True), (0x60, b"\xAB", True)])
    one_shot = I2cDecoder().decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    streaming = I2cDecoder()
    streaming.init({"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    out = streaming.feed(samples)
    out.extend(streaming.finalize())
    assert len(out) == len(one_shot)
    for a, b in zip(out, one_shot, strict=False):
        assert a.address == b.address
        assert a.data == b.data
        assert abs(a.timestamp_s - b.timestamp_s) < 1e-9


def test_streaming_arbitrary_chunk_boundaries() -> None:
    """Cutting the same buffer at varied positions must yield identical
    transactions. This proves the START/STOP edge detection works across
    chunk boundaries via the prev_sda/prev_scl carry."""
    samples = _i2c_samples([
        (0x50, b"\x01\x02", True),
        (0x60, b"\xAB", True),
        (0x70, b"\xCD\xEF", True),
    ])
    expected = I2cDecoder().decode(samples, {"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    n = samples.shape[0]
    for cut in (n // 7, n // 4, n // 3, n // 2, 2 * n // 3, 3 * n // 4):
        decoder = I2cDecoder()
        decoder.init({"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
        out = decoder.feed(samples[:cut])
        out.extend(decoder.feed(samples[cut:]))
        out.extend(decoder.finalize())
        assert len(out) == len(expected), f"cut={cut} produced {len(out)} != {len(expected)}"
        for a, b in zip(out, expected, strict=False):
            assert a.address == b.address, f"cut={cut} address mismatch"
            assert a.data == b.data, f"cut={cut} data mismatch"
            assert abs(a.timestamp_s - b.timestamp_s) < 1e-9, f"cut={cut} ts mismatch"


def test_streaming_chunk_inside_transaction_carries() -> None:
    """Cut a chunk in the middle of an I2C transaction. The transaction
    must be emitted on whichever feed() observes the STOP."""
    samples = _i2c_samples([(0x50, b"\x42", True)])
    # 'Right in the middle' — likely between SCL edges of a data byte.
    cut = samples.shape[0] // 2
    decoder = I2cDecoder()
    decoder.init({"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
    first = decoder.feed(samples[:cut])
    assert first == []  # STOP hasn't happened yet
    second = decoder.feed(samples[cut:])
    second.extend(decoder.finalize())
    assert len(second) == 1
    assert second[0].address == 0x50
    assert second[0].data == b"\x42"
