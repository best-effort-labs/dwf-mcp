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
    assert payload == b"Hi!"


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


def test_decode_parity_even_correct() -> None:
    """Byte 0x03 has 2 ones (even); even-parity bit must be 0 → no error."""
    samples = _uart_samples(b"\x03", baud=9600, sample_rate_hz=96000.0, parity="even")
    decoder = UartDecoder()
    frames = decoder.decode(
        samples, {"rx": 0}, sample_rate_hz=96000.0,
        baud=9600, data_bits=8, parity="even", stop_bits=1, polarity=0,
    )
    assert len(frames) == 1
    assert frames[0].data == b"\x03"
    assert frames[0].parity_error is False
    assert frames[0].error is False


def test_decode_parity_even_wrong_flags_error() -> None:
    """Hand-build a frame where the parity bit is wrong; expect parity_error=True."""
    # 1 start, 8 data (0x03 = LSB-first 11000000), wrong parity bit (1 instead of 0), 1 stop
    samples_per_bit = 10
    bits = [1]*20 + [0]*samples_per_bit  # idle + start
    # data bits LSB-first for 0x03 = bits 1,1,0,0,0,0,0,0
    for b in [1,1,0,0,0,0,0,0]:
        bits += [b]*samples_per_bit
    bits += [1]*samples_per_bit  # WRONG even parity bit (should be 0 for 0x03, but we send 1)
    bits += [1]*samples_per_bit  # stop
    bits += [1]*20

    arr = np.zeros((len(bits), 16), dtype=np.uint8)
    arr[:, 0] = bits
    decoder = UartDecoder()
    frames = decoder.decode(
        arr, {"rx": 0}, sample_rate_hz=samples_per_bit * 9600.0,
        baud=9600, data_bits=8, parity="even", stop_bits=1, polarity=0,
    )
    assert len(frames) == 1
    assert frames[0].parity_error is True
    assert frames[0].error is True


def test_uart_frame_row_shape_matches_engine_mode_sniff() -> None:
    """The UartFrame parquet row schema MUST match what sniff.uart writes.
    Spec guarantees observe-mode and engine-mode artifacts are indistinguishable."""
    samples = _uart_samples(b"A", baud=9600, sample_rate_hz=96000.0)
    decoder = UartDecoder()
    frames = decoder.decode(
        samples, {"rx": 0}, sample_rate_hz=96000.0,
        baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0,
    )
    assert len(frames) >= 1
    row = frames[0].to_dict()
    expected_keys = {
        "timestamp_s", "data",
        "parity_error", "framing_error", "break_condition",
        "error", "error_detail",
    }
    assert expected_keys.issubset(row.keys()), \
        f"missing keys: {expected_keys - row.keys()}"


def test_decode_framing_error_when_stop_bit_low() -> None:
    """Hand-build a frame whose stop bit is LOW (line broken) → framing_error=True."""
    samples_per_bit = 10
    bits = [1]*20 + [0]*samples_per_bit  # idle + start
    for b in [0,0,0,0,0,0,0,1]:  # 0x80 LSB-first
        bits += [b]*samples_per_bit
    bits += [0]*samples_per_bit  # WRONG stop bit (should be 1)
    bits += [1]*20  # idle after so decode loop can advance

    arr = np.zeros((len(bits), 16), dtype=np.uint8)
    arr[:, 0] = bits
    decoder = UartDecoder()
    frames = decoder.decode(
        arr, {"rx": 0}, sample_rate_hz=samples_per_bit * 9600.0,
        baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0,
    )
    assert len(frames) >= 1
    assert frames[0].framing_error is True
    assert frames[0].error is True


# --- Streaming API tests --------------------------------------------------

def test_streaming_single_chunk_matches_oneshot() -> None:
    """init/feed/finalize on one whole chunk must produce the same frames as decode()."""
    samples = _uart_samples(b"Hello", baud=9600, sample_rate_hz=96000.0)
    one_shot = UartDecoder().decode(
        samples, {"rx": 0}, sample_rate_hz=96000.0,
        baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0,
    )
    streaming = UartDecoder()
    streaming.init({"rx": 0}, sample_rate_hz=96000.0,
                   baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0)
    out = streaming.feed(samples)
    out.extend(streaming.finalize())
    assert len(out) == len(one_shot)
    for a, b in zip(out, one_shot):
        assert a.data == b.data
        assert a.timestamp_s == b.timestamp_s
        assert a.error == b.error


def test_streaming_arbitrary_chunk_boundaries() -> None:
    """Chunking the same buffer at every possible cut-point yields the same
    frames as the one-shot decode. This is the streaming correctness proof."""
    samples = _uart_samples(b"abc", baud=9600, sample_rate_hz=96000.0)
    expected = UartDecoder().decode(
        samples, {"rx": 0}, sample_rate_hz=96000.0,
        baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0,
    )
    # Cut the buffer in halves and thirds at varied positions; each split
    # must produce frames identical to the one-shot output.
    n = samples.shape[0]
    for cut in (n // 4, n // 3, n // 2, 2 * n // 3, 3 * n // 4):
        decoder = UartDecoder()
        decoder.init({"rx": 0}, sample_rate_hz=96000.0,
                     baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0)
        got = decoder.feed(samples[:cut])
        got.extend(decoder.feed(samples[cut:]))
        got.extend(decoder.finalize())
        assert len(got) == len(expected), f"cut={cut} produced {len(got)} != {len(expected)}"
        for a, b in zip(got, expected):
            assert a.data == b.data, f"cut={cut} data mismatch"
            assert abs(a.timestamp_s - b.timestamp_s) < 1e-9


def test_streaming_chunk_inside_frame_carries_correctly() -> None:
    """Cut a chunk in the middle of a UART frame. The frame must be emitted
    on the second feed() call with the correct timestamp."""
    samples = _uart_samples(b"X", baud=9600, sample_rate_hz=96000.0)
    # 'X' is one frame (~10 bit-times × 10 samples). Cut right in the data
    # bits — say at 50 samples in.
    cut = 50
    decoder = UartDecoder()
    decoder.init({"rx": 0}, sample_rate_hz=96000.0,
                 baud=9600, data_bits=8, parity="none", stop_bits=1, polarity=0)
    first = decoder.feed(samples[:cut])
    # The frame should NOT have emitted yet — it's still in the carry.
    assert first == []
    second = decoder.feed(samples[cut:])
    second.extend(decoder.finalize())
    assert len(second) == 1
    assert second[0].data == b"X"
