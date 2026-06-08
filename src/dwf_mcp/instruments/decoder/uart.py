"""Software UART decoder for raw DigitalIn captures."""
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder, UartFrame


class UartDecoder(Decoder):
    """Software UART decoder for raw DigitalIn captures.

    Locates start bits on the RX line, samples each subsequent bit at its
    mid-bit position, and emits one ``UartFrame`` per byte.

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

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        baud: int = 9600,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
        polarity: int = 0,
        **_unused: Any,
    ) -> list[UartFrame]:
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

        rx = samples[:, pin_map["rx"]].astype(np.uint8)
        if polarity == 1:
            rx = 1 - rx  # normalize to TTL convention: idle HIGH, start LOW

        idle, start_level = 1, 0
        frames: list[UartFrame] = []
        n = len(rx)
        i = 0

        def sample_at(start_i: int, bit_index: int) -> int:
            """Sample the line at the mid-point of bit ``bit_index`` relative
            to ``start_i`` (the first sample where rx == start_level)."""
            idx = int(start_i + samples_per_bit * (bit_index + 0.5))
            return int(rx[idx]) if idx < n else idle

        while i < n:
            # Skip ahead to the next start edge (line goes from idle to start_level).
            while i < n and rx[i] != start_level:
                i += 1
            if i >= n:
                break
            start_i = i

            # Re-validate the start bit at its mid-point. If it has already
            # returned to idle by then, it was a glitch — advance one sample
            # and resume the search.
            if sample_at(start_i, 0) != start_level:
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

            ts = start_i / sample_rate_hz
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
                error=par_err or fram_err,
                error_detail=error_detail,
            ))

            total_bits = 1 + data_bits + (1 if parity != "none" else 0) + stop_bits
            i = int(start_i + samples_per_bit * total_bits)

        return frames
