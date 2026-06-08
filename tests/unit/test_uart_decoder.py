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

    def push(b: int, n: int) -> None:
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


def test_decode_rejects_zero_sample_rate() -> None:
    decoder = UartDecoder()
    with pytest.raises(ValueError, match="sample_rate_hz must be positive"):
        decoder.decode(
            np.zeros((10, 16), dtype=np.uint8), {"rx": 0},
            sample_rate_hz=0,
            baud=9600,
        )
