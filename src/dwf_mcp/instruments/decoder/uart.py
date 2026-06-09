"""Software UART decoder for raw DigitalIn captures."""
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder, UartFrame


class UartDecoder(Decoder):
    """Software UART decoder for raw DigitalIn captures.

    Locates start bits on the RX line, samples each subsequent bit at its
    mid-bit position, and emits one ``UartFrame`` per byte.

    Supports streaming via ``init`` / ``feed`` / ``finalize`` (process chunks
    as they arrive) or one-shot via ``decode(samples)`` (the inherited
    convenience wrapper).

    Supported:
        - 5, 6, 7, or 8 data bits.
        - Parity ``"none"``, ``"even"``, or ``"odd"``.
        - 1 or 2 stop bits.
        - ``polarity=0`` (TTL convention: idle HIGH, start LOW) or
          ``polarity=1`` (inverted line; samples are inverted before decoding).

    Limitations:
        - Break-condition detection (sustained start-level for longer than
          one frame time) is NOT performed; a long low pulse is decoded as
          a 0x00 byte with a framing error.
        - Framing errors only flag a non-idle level at the first stop-bit
          position; the second stop bit (if any) is not validated.
        - Requires the sample rate to be at least 4x the configured baud
          rate (mid-bit sampling needs adequate margin).
    """

    protocol_name: ClassVar[str] = "uart"

    def init(  # type: ignore[override]
        self,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        baud: int = 9600,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
        polarity: int = 0,
        **_unused: Any,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError(
                f"sample_rate_hz must be positive, got {sample_rate_hz}"
            )
        if baud <= 0:
            raise ValueError(f"baud must be positive, got {baud}")
        if data_bits not in (5, 6, 7, 8):
            raise ValueError(f"data_bits must be 5, 6, 7, or 8, got {data_bits}")
        if parity not in ("none", "even", "odd"):
            raise ValueError(f"parity must be 'none', 'even', or 'odd', got {parity!r}")
        if stop_bits not in (1, 2):
            raise ValueError(f"stop_bits must be 1 or 2, got {stop_bits}")
        if polarity not in (0, 1):
            raise ValueError(f"polarity must be 0 or 1, got {polarity}")

        samples_per_bit = sample_rate_hz / baud
        if samples_per_bit < 4:
            raise ValueError(
                f"UART decode requires >=4x oversampling, got {samples_per_bit:.1f}x "
                f"(sample_rate_hz={sample_rate_hz}, baud={baud})"
            )

        self._rx_col = pin_map["rx"]
        self._sample_rate_hz = sample_rate_hz
        self._baud = baud
        self._data_bits = data_bits
        self._parity = parity
        self._stop_bits = stop_bits
        self._polarity = polarity
        self._samples_per_bit = samples_per_bit
        self._total_bits = (
            1 + data_bits + (1 if parity != "none" else 0) + stop_bits
        )
        self._consumed_total = 0  # absolute index of self._carry[0]
        self._carry = np.zeros(0, dtype=np.uint8)

    def feed(self, samples: np.ndarray) -> list[UartFrame]:  # type: ignore[override]
        chunk_rx = samples[:, self._rx_col].astype(np.uint8)
        if self._polarity == 1:
            chunk_rx = 1 - chunk_rx
        if len(self._carry):
            working = np.concatenate([self._carry, chunk_rx])
        else:
            working = chunk_rx
        frames, consumed = self._scan(working)
        # consumed samples are now permanently behind us; advance the
        # offset and keep only the tail as carry.
        self._consumed_total += consumed
        self._carry = working[consumed:].copy()  # detach from working
        return frames

    def finalize(self) -> list[UartFrame]:  # type: ignore[override]
        # Any samples still in self._carry didn't form a complete frame.
        # Drop them; partial UART frames at end-of-stream are not emitted.
        self._carry = np.zeros(0, dtype=np.uint8)
        return []

    # --- internal -----------------------------------------------------------

    def _scan(self, rx: np.ndarray) -> tuple[list[UartFrame], int]:
        """Scan ``rx`` for complete UART frames. Returns (frames, consumed)
        where ``consumed`` is the index up to (but not including) which all
        samples are definitively processed. ``rx[consumed:]`` is the tail
        that may form the start of a future frame and must be carried."""
        idle, start_level = 1, 0
        n = len(rx)
        samples_per_bit = self._samples_per_bit
        data_bits = self._data_bits
        parity = self._parity
        stop_bits = self._stop_bits
        total_bits_needed = self._total_bits
        base_ts = self._consumed_total / self._sample_rate_hz

        def sample_at(start_i: int, bit_index: int) -> int:
            idx = int(start_i + samples_per_bit * (bit_index + 0.5))
            return int(rx[idx]) if idx < n else idle

        frames: list[UartFrame] = []
        i = 0
        consumed = 0  # samples we definitively don't need to revisit
        while i < n:
            while i < n and rx[i] != start_level:
                i += 1
            if i >= n:
                # No start in remaining buffer — everything up to here is
                # consumed (all idle, never going to retroactively decode).
                consumed = n
                break
            start_i = i
            if start_i + samples_per_bit * (total_bits_needed - 0.5) >= n:
                # Found a potential start but the frame doesn't fit in this
                # buffer. Carry from start_i onward.
                consumed = start_i
                break
            if sample_at(start_i, 0) != start_level:
                # Glitch — drop the single sample and resume search.
                i += 1
                continue
            byte = 0
            for b in range(data_bits):
                byte |= sample_at(start_i, 1 + b) << b
            par_err = False
            if parity != "none":
                par_bit = sample_at(start_i, 1 + data_bits)
                ones = bin(byte).count("1") + par_bit
                if parity == "even" and (ones & 1) != 0:
                    par_err = True
                if parity == "odd" and (ones & 1) != 1:
                    par_err = True
            stop_index = 1 + data_bits + (1 if parity != "none" else 0)
            fram_err = sample_at(start_i, stop_index) != idle
            ts = base_ts + start_i / self._sample_rate_hz
            error_detail: str | None = None
            if par_err:
                error_detail = "parity error"
            elif fram_err:
                error_detail = "framing error"
            frames.append(UartFrame(
                timestamp_s=ts,
                data=bytes([byte]),
                parity_error=par_err,
                framing_error=fram_err,
                break_condition=None,
                error=par_err or fram_err,
                error_detail=error_detail,
            ))
            # Frame samples past the buffer end (the stop bit's tail) are OOB
            # but were treated as idle by sample_at, so the frame is still
            # correctly decoded. Don't advance `consumed` past the actual
            # buffer length — the next chunk will pick up from the true
            # boundary and re-scan any trailing samples that DO exist.
            i = min(int(start_i + samples_per_bit * total_bits_needed), n)
            consumed = i
        return frames, consumed
