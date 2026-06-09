from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.spi import SpiDecoder


def _spi_samples(
    words: list[int],
    word_size: int = 8,
    bit_order: str = "msb",
    mode: int = 0,
    sph: int = 5,
    with_miso: bool = True,
    with_cs: bool = True,
) -> tuple[np.ndarray, dict[str, int]]:
    """Build a synthetic (n, 16) uint8 SPI capture.

    Column layout: CLK=0, MOSI=1, MISO=2 (loopback = MOSI), CS=3 (active-low).
    Mode 0 (CPOL=0,CPHA=0): idle CLK=0, sample on rising edge.
    Mode 3 (CPOL=1,CPHA=1): idle CLK=1, sample on falling edge (active phase entry).
    """
    cpol = mode >> 1

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

        # CS asserts; first bit pre-loaded on MOSI (CPHA=0 / CPHA=1 both handled same way here)
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


SAMPLE_RATE = 1_000_000.0


def test_mode0_single_byte() -> None:
    samples, pin_map = _spi_samples([0xA5])
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 1
    assert txns[0].mosi == bytes([0xA5])
    assert txns[0].miso == bytes([0xA5])   # loopback
    assert txns[0].cs_active is True
    assert txns[0].cs_error is False
    assert txns[0].word_index == 0


def test_mode0_two_bytes() -> None:
    samples, pin_map = _spi_samples([0xA5, 0x5A])
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 2
    assert txns[0].mosi == bytes([0xA5])
    assert txns[1].mosi == bytes([0x5A])
    assert txns[1].word_index == 1


def test_mode3_single_byte() -> None:
    samples, pin_map = _spi_samples([0x42], mode=3)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=3)
    assert len(txns) == 1
    assert txns[0].mosi == bytes([0x42])


def test_no_miso() -> None:
    samples, pin_map = _spi_samples([0xBE], with_miso=False)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 1
    assert txns[0].miso is None


def test_no_cs() -> None:
    samples, pin_map = _spi_samples([0xFF], with_cs=False)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert len(txns) == 1
    assert txns[0].cs_active is True
    assert txns[0].cs_error is False


def test_timestamp_nonzero() -> None:
    samples, pin_map = _spi_samples([0x01])
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0)
    assert txns[0].timestamp_s > 0.0


def test_lsb_first() -> None:
    samples, pin_map = _spi_samples([0x01], bit_order="lsb")
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=0, bit_order="lsb")
    assert txns[0].mosi == bytes([0x01])


def test_mode1_single_byte() -> None:
    samples, pin_map = _spi_samples([0x37], mode=1)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=1)
    assert len(txns) == 1
    assert txns[0].mosi == bytes([0x37])


def test_mode2_single_byte() -> None:
    samples, pin_map = _spi_samples([0xC3], mode=2)
    decoder = SpiDecoder()
    txns = decoder.decode(samples, pin_map, sample_rate_hz=SAMPLE_RATE, mode=2)
    assert len(txns) == 1
    assert txns[0].mosi == bytes([0xC3])


# --- Streaming API tests --------------------------------------------------

def test_streaming_single_chunk_matches_oneshot() -> None:
    samples, _ = _spi_samples([0xA5, 0x5A], mode=0)
    one_shot = SpiDecoder().decode(
        samples, {"clk": 0, "mosi": 1, "miso": 2, "cs": 3},
        sample_rate_hz=10_000_000.0, mode=0,
    )
    streaming = SpiDecoder()
    streaming.init(
        {"clk": 0, "mosi": 1, "miso": 2, "cs": 3},
        sample_rate_hz=10_000_000.0, mode=0,
    )
    out = streaming.feed(samples)
    out.extend(streaming.finalize())
    assert len(out) == len(one_shot)
    for a, b in zip(out, one_shot):
        assert a.mosi == b.mosi
        assert a.word_index == b.word_index
        assert abs(a.timestamp_s - b.timestamp_s) < 1e-9


def test_streaming_arbitrary_chunk_boundaries() -> None:
    """Multi-word SPI buffer cut at varied positions yields identical words."""
    samples, _ = _spi_samples([0xA5, 0x5A, 0xFF, 0x00], mode=0)
    expected = SpiDecoder().decode(
        samples, {"clk": 0, "mosi": 1, "miso": 2, "cs": 3},
        sample_rate_hz=10_000_000.0, mode=0,
    )
    n = samples.shape[0]
    for cut in (n // 5, n // 4, n // 3, n // 2, 2 * n // 3, 3 * n // 4):
        decoder = SpiDecoder()
        decoder.init(
            {"clk": 0, "mosi": 1, "miso": 2, "cs": 3},
            sample_rate_hz=10_000_000.0, mode=0,
        )
        out = decoder.feed(samples[:cut])
        out.extend(decoder.feed(samples[cut:]))
        out.extend(decoder.finalize())
        assert len(out) == len(expected), f"cut={cut} count mismatch"
        for a, b in zip(out, expected):
            assert a.mosi == b.mosi, f"cut={cut} mosi mismatch"
            assert a.word_index == b.word_index
