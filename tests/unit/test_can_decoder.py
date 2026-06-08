"""Synthetic CAN samples → CanDecoder → assert decoded frames."""
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.can import CanDecoder, can_crc15


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
    crc_input = bits[1:]   # exclude SOF from CRC
    crc = can_crc15(crc_input)
    for i in range(15):
        bits.append((crc >> (14 - i)) & 1)
    bits.append(1)  # CRC delim
    bits.append(1)  # ACK slot (no ack from passive listener)
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
    rx.extend([1] * samples_per_bit * 15)  # idle (recessive)
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


def test_decode_rejects_zero_sample_rate() -> None:
    decoder = CanDecoder()
    with pytest.raises(ValueError, match="sample_rate_hz must be positive"):
        decoder.decode(
            np.zeros((10, 16), dtype=np.uint8), {"rx": 0},
            sample_rate_hz=0, bitrate=100_000,
        )
