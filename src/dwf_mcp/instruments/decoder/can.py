"""Software CAN decoder for raw DigitalIn captures (Classical CAN; 11- or 29-bit IDs)."""
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
        - Standard 11-bit identifiers (IDE=0) and extended 29-bit identifiers
          (IDE=1, with the 18 extra arbitration bits + SRR + r1).
        - DLC 0-8 with the matching data field. Raw DLC values 9-15 are
          preserved in the decoded frame but flagged ``error_type="form"``;
          the data field is read as 8 bytes (per classic CAN spec).
        - CRC-15 (polynomial 0x4599) validation.
        - Bit-stuffing destuffing through the CRC field.

    Out of scope (not implemented; would require additional state):
        - CAN FD frames (BRS, ESI, 17/21-bit CRC).
        - Error / overload frame detection.
        - ACK delimiter validation.
        - Sample-point tuning (fixed at 75 %).
    """

    protocol_name: ClassVar[str] = "can"

    # Worst-case CAN frame length: extended ID + 8 data bytes + every
    # possible stuff bit through the CRC region + post-CRC fields + IFS.
    # 200 bit-times is comfortably above the maximum.
    _WORST_CASE_FRAME_BITS: ClassVar[int] = 200

    def init(
        self,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        bitrate: int = 100_000,
        **_unused: Any,
    ) -> None:
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
        self._rx_col = pin_map["rx"]
        self._sample_rate_hz = sample_rate_hz
        self._bitrate = bitrate
        self._samples_per_bit = samples_per_bit
        self._sample_point = int(round(samples_per_bit * 0.75))
        self._consumed_total = 0
        self._carry = np.zeros(0, dtype=np.uint8)

    def feed(self, samples: np.ndarray) -> list[CanFrame]:
        chunk_rx = samples[:, self._rx_col].astype(np.uint8)
        if len(self._carry):
            rx = np.concatenate([self._carry, chunk_rx])
        else:
            rx = chunk_rx
        frames, consumed = self._scan(rx)
        self._consumed_total += consumed
        self._carry = rx[consumed:].copy()
        return frames

    def finalize(self) -> list[CanFrame]:
        # No more samples coming — make a best-effort decode of anything
        # still in the carry, treating OOB sample positions as recessive
        # (which is how the pre-streaming one-shot decode behaved at the
        # end of its buffer). This is what makes the streaming wrapper
        # equivalent to one-shot for small buffers that don't have the
        # worst-case 200-bit headroom past every SOF.
        rx = self._carry
        self._carry = np.zeros(0, dtype=np.uint8)
        if len(rx) == 0:
            return []
        frames, consumed = self._scan(rx, allow_partial=True)
        self._consumed_total += consumed
        return frames

    def _scan(
        self, rx: np.ndarray, allow_partial: bool = False,
    ) -> tuple[list[CanFrame], int]:
        """Scan ``rx`` for complete CAN frames. Returns (frames, consumed)
        where rx[consumed:] is the tail to carry into the next feed call.

        When ``allow_partial`` is True (set by ``finalize``), the worst-case
        headroom check is skipped — sample_bit's OOB recessive return makes
        any truncated trailing frame decode as if padded with idle, matching
        the pre-streaming one-shot behavior.
        """
        samples_per_bit = self._samples_per_bit
        sample_point = self._sample_point
        sample_rate_hz = self._sample_rate_hz
        base_ts = self._consumed_total / sample_rate_hz
        worst_case_samples = int(samples_per_bit * self._WORST_CASE_FRAME_BITS)
        n = len(rx)

        frames: list[CanFrame] = []
        i = 0
        consumed = 0
        while i < n:
            while i < n and rx[i] == 1:
                i += 1
            if i >= n:
                consumed = n
                break
            sof_i = i
            if not allow_partial and n - sof_i < worst_case_samples:
                # Found a SOF candidate but a worst-case frame wouldn't fit
                # in the remaining buffer. Carry from sof_i and wait for
                # more samples on a future feed() call (or for finalize()
                # to drain with allow_partial=True).
                consumed = sof_i
                break
            ts = base_ts + sof_i / sample_rate_hz

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
                i = sof_i + max(1, int(samples_per_bit * 16))
                consumed = min(i, n)
                continue

            trailing = 1 + 1 + 1 + 7 + 3 + 4  # CRC delim + ACK + ACK delim + EOF + IFS + slack
            advance = max(1, int(samples_per_bit * (bits_through_crc + trailing)))
            i = sof_i + advance
            consumed = min(i, n)
        return frames, consumed


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
    error frames, FD, etc.)."""
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
    base_id = 0
    for _ in range(11):
        base_id = (base_id << 1) | next_logical()
    # This bit is RTR for standard frames, SRR (substitute remote request,
    # always recessive=1) for extended frames. The IDE bit that follows
    # disambiguates.
    rtr_or_srr = next_logical()
    ide = next_logical()
    if ide == 0:
        # Standard 11-bit frame
        frame_id = base_id
        extended = False
        rtr = bool(rtr_or_srr)
        _ = next_logical()  # r0 (reserved)
    else:
        # Extended 29-bit frame: 18 more ID bits, then real RTR + r1 + r0.
        ext_id = 0
        for _ in range(18):
            ext_id = (ext_id << 1) | next_logical()
        frame_id = (base_id << 18) | ext_id
        extended = True
        rtr = bool(next_logical())
        _ = next_logical()  # r1 (reserved)
        _ = next_logical()  # r0 (reserved)
    dlc_raw = 0
    for _ in range(4):
        dlc_raw = (dlc_raw << 1) | next_logical()
    # The wire value of DLC may be 9–15 on classic CAN; the data field is
    # still capped at 8 bytes by the spec. Preserve the raw value in the
    # decoded frame but only read 8 bytes of data, and flag this as a form
    # error so callers can distinguish it from a well-formed frame.
    data_field_len = min(dlc_raw, 8)
    dlc_over_8 = dlc_raw > 8
    data_bytes = bytearray()
    # Per CAN spec, when RTR=1 no data field is transmitted regardless of DLC.
    if not rtr:
        for _ in range(data_field_len):
            byte = 0
            for _ in range(8):
                byte = (byte << 1) | next_logical()
            data_bytes.append(byte)

    crc_input = consumed[1:]  # exclude SOF; uses the actually-transmitted bits
    expected_crc = can_crc15(crc_input)

    rx_crc = 0
    for _ in range(15):
        rx_crc = (rx_crc << 1) | next_logical()
    crc_valid = (rx_crc == expected_crc)

    if dlc_over_8:
        error_type: str | None = "form"
        error_flag = True
        error_detail: str | None = (
            f"DLC={dlc_raw} > 8 (classic CAN max); data field clamped to 8 bytes"
        )
    elif not crc_valid:
        error_type = "crc"
        error_flag = True
        error_detail = "crc mismatch"
    else:
        error_type = None
        error_flag = False
        error_detail = None

    frame = CanFrame(
        timestamp_s=timestamp_s,
        frame_id=frame_id,
        extended=extended,
        rtr=rtr,
        dlc=dlc_raw,
        data=bytes(data_bytes),
        crc_valid=crc_valid,
        ack_received=None,
        error_type=error_type,
        error=error_flag,
        error_detail=error_detail,
    )
    return frame, state["bit_idx"]
