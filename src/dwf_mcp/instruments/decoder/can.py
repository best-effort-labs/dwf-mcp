"""Software CAN decoder for raw DigitalIn captures (Standard 11-bit IDs)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.instruments.decoder.base import CanFrame, Decoder

CAN_CRC15_POLY = 0x4599


def can_crc15(bits: list[int]) -> int:
    """Compute the CAN CRC-15 over an iterable of 0/1 bits.

    Implements the standard left-shift LFSR with polynomial 0x4599
    (x^15 + x^14 + x^10 + x^8 + x^7 + x^4 + x^3 + 1). By CAN convention
    the CRC is computed over the de-stuffed arbitration + control + data
    fields — i.e. everything after SOF and before the CRC field itself.
    Callers are responsible for slicing the input accordingly.
    """
    crc = 0
    for bit in bits:
        nxt = (crc >> 14) ^ (bit & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= CAN_CRC15_POLY
    return crc & 0x7FFF


class CanDecoder(Decoder):
    """Software CAN decoder for raw DigitalIn captures.

    Walks the RX line one bit-time at a time, sampling each bit at 75 % of
    the bit-time (the CiA-recommended "second sample point"), removes
    bit-stuffing on the fly, and emits one ``CanFrame`` per Start-of-Frame.

    Bus convention assumed by this decoder:
        - dominant = 0 (active drive)
        - recessive = 1 (idle)
        Users with an inverting transceiver must invert upstream.

    Supported:
        - Standard 11-bit identifiers.
        - DLC 0-8 with the matching data field.
        - CRC-15 (polynomial 0x4599) validation.
        - Bit-stuffing destuffing through the CRC field.

    Out of scope (not implemented; would require additional state):
        - CAN FD frames (BRS, ESI, 17/21-bit CRC).
        - 29-bit extended identifiers (IDE=1 raises a form error).
        - Error / overload frame detection.
        - ACK delimiter validation.
        - Sample-point tuning (fixed at 75 %).
    """

    protocol_name: ClassVar[str] = "can"

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        bitrate: int = 100_000,
        **_unused: Any,
    ) -> list[CanFrame]:
        if sample_rate_hz <= 0:
            raise ValueError(
                f"sample_rate_hz must be positive, got {sample_rate_hz}"
            )
        if bitrate <= 0:
            raise ValueError(f"bitrate must be positive, got {bitrate}")
        samples_per_bit = sample_rate_hz / bitrate
        if samples_per_bit < 8:
            raise ValueError(
                f"CAN decode requires >=8x oversampling, got {samples_per_bit:.1f}x "
                f"(sample_rate_hz={sample_rate_hz}, bitrate={bitrate})"
            )
        sample_point = int(round(samples_per_bit * 0.75))

        rx = samples[:, pin_map["rx"]].astype(np.uint8)
        frames: list[CanFrame] = []
        n = len(rx)
        i = 0
        while i < n:
            # Scan to the next dominant (SOF candidate).
            while i < n and rx[i] == 1:
                i += 1
            if i >= n:
                break
            sof_i = i
            ts = sof_i / sample_rate_hz

            def sample_bit(bit_index: int, _base: int = sof_i) -> int:
                idx = _base + int(samples_per_bit * bit_index) + sample_point
                return int(rx[idx]) if 0 <= idx < n else 1

            try:
                frame, bits_through_crc = _parse_can_frame(sample_bit, ts)
                frames.append(frame)
            except _CanParseError as exc:
                frames.append(CanFrame(
                    timestamp_s=ts,
                    frame_id=0,
                    extended=False,
                    rtr=False,
                    dlc=0,
                    data=b"",
                    crc_valid=None,
                    ack_received=None,
                    error_type=exc.kind,
                    error=True,
                    error_detail=str(exc),
                ))
                # Skip a conservative number of bit-times so we resync past
                # the partial frame and look for the next dominant edge.
                i = sof_i + max(1, int(samples_per_bit * 16))
                continue

            # Advance past every bit the parser actually read (which already
            # accounts for stuff bits in the SOF→CRC region), plus the
            # post-CRC fields (CRC delim + ACK + ACK delim + 7 EOF + 3 IFS).
            # An extra few bit-times of slack absorbs any spurious stuff
            # bits a noisy / non-compliant transmitter may emit in EOF.
            trailing = 1 + 1 + 1 + 7 + 3 + 4  # +4 slack
            advance = max(1, int(samples_per_bit * (bits_through_crc + trailing)))
            i = sof_i + advance

        return frames


class _CanParseError(Exception):
    def __init__(self, kind: str, msg: str = "") -> None:
        super().__init__(msg or kind)
        self.kind = kind


def _parse_can_frame(
    sample_bit: Callable[[int], int],
    timestamp_s: float,
) -> tuple[CanFrame, int]:
    """Pull a logical (destuffed) bit at a time from ``sample_bit`` and
    assemble a standard CAN frame.

    Returns the frame plus the number of physical bits actually consumed
    by the parser (SOF through CRC, including stuff bits) — the caller
    uses this to advance past the frame without re-counting stuffing.

    Raises ``_CanParseError`` on protocol violations (missing stuff bit,
    extended IDs, etc.)."""
    state = {"bit_idx": 0, "last": -1, "run": 0}
    consumed: list[int] = []

    def next_logical() -> int:
        v = sample_bit(state["bit_idx"])
        state["bit_idx"] += 1
        if v == state["last"]:
            state["run"] += 1
        else:
            state["last"] = v
            state["run"] = 1
        if state["run"] == 5:
            # After 5 consecutive identical bits, the transmitter inserts
            # one opposite bit. Read and discard it; if it matches v then
            # the line violated the stuffing rule.
            stuff = sample_bit(state["bit_idx"])
            state["bit_idx"] += 1
            if stuff == v:
                raise _CanParseError("stuff", "missing stuff bit")
            state["last"] = stuff
            state["run"] = 1
        consumed.append(v)
        return v

    if next_logical() != 0:
        raise _CanParseError("form", "SOF not dominant")
    frame_id = 0
    for _ in range(11):
        frame_id = (frame_id << 1) | next_logical()
    rtr = bool(next_logical())
    ide = next_logical()
    if ide != 0:
        raise _CanParseError("form", "extended IDs not supported")
    _ = next_logical()  # r0 (reserved)
    dlc_raw = 0
    for _ in range(4):
        dlc_raw = (dlc_raw << 1) | next_logical()
    dlc = min(dlc_raw, 8)
    data_bytes = bytearray()
    # Per CAN spec, when RTR=1 no data field is transmitted regardless of DLC.
    if not rtr:
        for _ in range(dlc):
            byte = 0
            for _ in range(8):
                byte = (byte << 1) | next_logical()
            data_bytes.append(byte)

    crc_input = consumed[1:]  # exclude SOF
    expected_crc = can_crc15(crc_input)

    rx_crc = 0
    for _ in range(15):
        rx_crc = (rx_crc << 1) | next_logical()
    crc_valid = (rx_crc == expected_crc)

    frame = CanFrame(
        timestamp_s=timestamp_s,
        frame_id=frame_id,
        extended=False,
        rtr=rtr,
        dlc=dlc,
        data=bytes(data_bytes),
        crc_valid=crc_valid,
        ack_received=None,
        error_type=None if crc_valid else "crc",
        error=not crc_valid,
        error_detail=None if crc_valid else "crc mismatch",
    )
    return frame, state["bit_idx"]
