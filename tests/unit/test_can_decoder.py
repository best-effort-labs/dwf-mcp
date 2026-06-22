"""Synthetic CAN samples → CanDecoder → assert decoded frames."""
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.instruments.decoder.can import CanDecoder, can_crc15


def _can_bits(
    frame_id: int, data: bytes, rtr: bool = False, extended: bool = False,
) -> list[int]:
    """Build the raw bit stream for a Classical CAN frame (no bit-stuffing yet).

    Standard frame layout: SOF | ID[10:0] | RTR | IDE=0 | r0 | DLC[3:0] | DATA |
        CRC15 | CRC delim | ACK | ACK delim | EOF×7
    Extended frame layout: SOF | base ID[10:0] | SRR=1 | IDE=1 | ext ID[17:0] | RTR |
        r1 | r0 | DLC[3:0] | DATA | CRC15 | …
    """
    bits: list[int] = []
    bits.append(0)  # SOF dominant
    if extended:
        base = (frame_id >> 18) & 0x7FF
        ext = frame_id & 0x3FFFF
        for i in range(11):
            bits.append((base >> (10 - i)) & 1)
        bits.append(1)  # SRR (always recessive in extended frames)
        bits.append(1)  # IDE = 1
        for i in range(18):
            bits.append((ext >> (17 - i)) & 1)
        bits.append(1 if rtr else 0)  # RTR
        bits.append(0)  # r1
        bits.append(0)  # r0
    else:
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


def test_decode_extended_29bit_frame() -> None:
    """29-bit identifier (IDE=1) decodes with extended=True and the full ID."""
    # 0x1A2B3C4 has bits in both the base (top 11) and extension (low 18) halves.
    fid = 0x1A2B3C4
    bits = _stuff(_can_bits(fid, b"\xCA\xFE", extended=True))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert len(frames) == 1
    assert frames[0].frame_id == fid
    assert frames[0].extended is True
    assert frames[0].rtr is False
    assert frames[0].data == b"\xCA\xFE"
    assert frames[0].crc_valid is True
    assert frames[0].error is False


def test_decode_extended_rtr_frame() -> None:
    """Extended remote frame: extended=True, rtr=True, no data."""
    bits = _stuff(_can_bits(0x1ABCDEF, b"", rtr=True, extended=True))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert len(frames) == 1
    assert frames[0].extended is True
    assert frames[0].rtr is True
    assert frames[0].frame_id == 0x1ABCDEF
    assert frames[0].data == b""
    assert frames[0].error is False


def test_decode_extended_full_id_range() -> None:
    """Full 29-bit ID (all bits set) decodes correctly."""
    bits = _stuff(_can_bits(0x1FFFFFFF, b"\x00", extended=True))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert frames[0].frame_id == 0x1FFFFFFF
    assert frames[0].extended is True


def test_decode_mixes_standard_and_extended_frames() -> None:
    """A buffer with one standard then one extended frame decodes both correctly."""
    std_bits = _stuff(_can_bits(0x123, b"\x11"))
    ext_bits = _stuff(_can_bits(0x1A2B3C4, b"\x22", extended=True))
    samples_std = _samples_from_bits(std_bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    samples_ext = _samples_from_bits(ext_bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    combined = np.concatenate([samples_std, samples_ext], axis=0)
    decoder = CanDecoder()
    frames = decoder.decode(combined, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    good = [f for f in frames if not f.error]
    assert len(good) >= 2
    by_id = {(f.extended, f.frame_id): f for f in good}
    assert (False, 0x123) in by_id
    assert (True, 0x1A2B3C4) in by_id


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


def test_decode_rtr_frame_has_no_data_field() -> None:
    """RTR frames have DLC set but no data on the wire."""
    # RTR=True: _can_bits emits DLC but no data bytes
    bits = _stuff(_can_bits(0x200, b"", rtr=True))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert len(frames) == 1
    assert frames[0].rtr is True
    assert frames[0].data == b""
    assert frames[0].error is False


def test_decode_crc_mismatch_flagged_as_error() -> None:
    """Tamper with one CRC bit; decoder must report crc_valid=False, error_type='crc'."""
    bits = _can_bits(0x123, b"\xAA")
    # Find a CRC bit (last 15 bits before CRC delim @ position -8) and flip it.
    # _can_bits layout: ... data (8 bits per byte), CRC (15 bits), CRC delim, ACK, ACK delim, 7 EOF
    # CRC bits span [-23:-8]. Flip bit at index -16 (mid-CRC).
    bits[-16] = 1 - bits[-16]
    bits = _stuff(bits)
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert len(frames) == 1
    assert frames[0].crc_valid is False
    assert frames[0].error is True
    assert frames[0].error_type == "crc"


def test_decode_back_to_back_frames() -> None:
    """Two frames in one buffer; the decoder must resync and emit both."""
    bits1 = _stuff(_can_bits(0x100, b"\x11"))
    bits2 = _stuff(_can_bits(0x200, b"\x22\x33"))
    # _samples_from_bits already pads each side with 15 bit-times of
    # recessive idle, so concatenating two such buffers gives ~30 bit-times
    # of interframe space — well above the 11-bit IFS minimum.
    samples1 = _samples_from_bits(bits1, bitrate=100_000, sample_rate_hz=2_000_000.0)
    samples2 = _samples_from_bits(bits2, bitrate=100_000, sample_rate_hz=2_000_000.0)
    combined = np.concatenate([samples1, samples2], axis=0)
    decoder = CanDecoder()
    frames = decoder.decode(combined, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    # The all-recessive idle on each end + the EOF/IFS between is enough resync.
    ids = sorted(f.frame_id for f in frames if not f.error)
    assert 0x100 in ids and 0x200 in ids


def test_decode_dlc_over_8_flagged_as_form_error() -> None:
    """DLC=9 wire value: read 8 data bytes but flag error_type='form'."""
    # Manually construct bits with DLC=9 (binary 1001).
    bits = [0]  # SOF
    fid = 0x123
    for i in range(11):
        bits.append((fid >> (10 - i)) & 1)
    bits.append(0)  # RTR
    bits.append(0)  # IDE
    bits.append(0)  # r0
    # DLC = 9 = 0b1001
    for b in [1, 0, 0, 1]:
        bits.append(b)
    # 8 data bytes
    data = b"\x00\x01\x02\x03\x04\x05\x06\x07"
    for byte in data:
        for i in range(8):
            bits.append((byte >> (7 - i)) & 1)
    # CRC over arbitration+control+data (excluding SOF)
    crc = can_crc15(bits[1:])
    for i in range(15):
        bits.append((crc >> (14 - i)) & 1)
    bits.append(1)  # CRC delim
    bits.append(1)  # ACK slot
    bits.append(1)  # ACK delim
    bits.extend([1] * 7)  # EOF
    bits = _stuff(bits)

    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    decoder = CanDecoder()
    frames = decoder.decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    assert len(frames) == 1
    assert frames[0].dlc == 9, f"raw DLC should be reported, got {frames[0].dlc}"
    assert len(frames[0].data) == 8
    assert frames[0].error is True
    assert frames[0].error_type == "form"


def test_can_crc15_known_vector() -> None:
    """Smoke against a hand-computed CRC vector to catch polynomial errors.

    Bits fed into the LFSR (excluding SOF): ID=0x123 MSB-first (11 bits),
    RTR=0, IDE=0, r0=0, DLC=0 (4 bits) = 18 bits total.

    Independent reference computed by tracing the polynomial 0x4599 LFSR
    bit-by-bit (verified with three separate implementations). If THIS
    test breaks, can_crc15's polynomial or shift direction is wrong.
    """
    bits_after_sof = (
        [0,0,1,0,0,1,0,0,0,1,1]  # ID 0x123 MSB-first
        + [0]                       # RTR
        + [0]                       # IDE
        + [0]                       # r0
        + [0,0,0,0]                 # DLC = 0
    )
    expected = 0x6858
    actual = can_crc15(bits_after_sof)
    assert actual == expected, f"CRC15(0x123/empty) expected {expected:#06x}, got {actual:#06x}"


# --- Streaming API tests --------------------------------------------------

def test_streaming_single_chunk_matches_oneshot() -> None:
    bits = _stuff(_can_bits(0x123, b"\xDE\xAD"))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    one_shot = CanDecoder().decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    streaming = CanDecoder()
    streaming.init({"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    out = streaming.feed(samples)
    out.extend(streaming.finalize())
    assert len(out) == len(one_shot)
    for a, b in zip(out, one_shot, strict=False):
        assert a.frame_id == b.frame_id
        assert a.data == b.data
        assert abs(a.timestamp_s - b.timestamp_s) < 1e-9


def test_streaming_arbitrary_chunk_boundaries() -> None:
    """Multi-frame buffer cut at varied positions yields identical frames."""
    bits1 = _stuff(_can_bits(0x100, b"\x11"))
    bits2 = _stuff(_can_bits(0x200, b"\x22\x33"))
    bits3 = _stuff(_can_bits(0x1A2B3C4, b"\x44", extended=True))
    s1 = _samples_from_bits(bits1, bitrate=100_000, sample_rate_hz=2_000_000.0)
    s2 = _samples_from_bits(bits2, bitrate=100_000, sample_rate_hz=2_000_000.0)
    s3 = _samples_from_bits(bits3, bitrate=100_000, sample_rate_hz=2_000_000.0)
    samples = np.concatenate([s1, s2, s3], axis=0)
    expected = CanDecoder().decode(samples, {"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
    expected_good = [(f.extended, f.frame_id) for f in expected if not f.error]
    n = samples.shape[0]
    for cut in (n // 5, n // 4, n // 3, n // 2, 2 * n // 3):
        decoder = CanDecoder()
        decoder.init({"rx": 0}, sample_rate_hz=2_000_000.0, bitrate=100_000)
        out = decoder.feed(samples[:cut])
        out.extend(decoder.feed(samples[cut:]))
        out.extend(decoder.finalize())
        got_good = [(f.extended, f.frame_id) for f in out if not f.error]
        assert got_good == expected_good, f"cut={cut} ids {got_good} != {expected_good}"
