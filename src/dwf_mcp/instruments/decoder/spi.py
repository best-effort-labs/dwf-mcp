from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from dwf_mcp.instruments.decoder.base import Decoder, SpiTransaction

# (CPOL, CPHA) -> sample on rising edge?
# Convention follows the test generator: active_clk = 1 - cpol.
# Data is stable during the active phase; sampling occurs on entry into active.
# CPOL=0 → active=HIGH → entry edge is RISING  → sample_on_rising=True  (modes 0 and 1)
# CPOL=1 → active=LOW  → entry edge is FALLING → sample_on_rising=False (modes 2 and 3)
_SAMPLE_ON_RISING: dict[tuple[int, int], bool] = {
    (0, 0): True,
    (0, 1): True,
    (1, 0): False,
    (1, 1): False,
}


class SpiDecoder(Decoder):
    """Software SPI decoder for raw DigitalIn captures.

    Supports streaming via ``init`` / ``feed`` / ``finalize`` (process chunks
    as they arrive) or one-shot via ``decode(samples)``.
    """

    protocol_name: ClassVar[str] = "spi"

    def init(
        self,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        mode: int = 0,
        bit_order: str = "msb",
        word_size: int = 8,
        **_: Any,
    ) -> None:
        if mode not in (0, 1, 2, 3):
            raise ValueError(f"mode must be 0-3, got {mode!r}")
        if sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz must be positive, got {sample_rate_hz}")
        if word_size < 1 or word_size > 32:
            raise ValueError(f"word_size must be 1..32, got {word_size}")
        if bit_order not in ("msb", "lsb"):
            raise ValueError(f"bit_order must be 'msb' or 'lsb', got {bit_order!r}")

        self._sample_rate_hz = sample_rate_hz
        cpol = mode >> 1
        cpha = mode & 1
        self._sample_on_rising = _SAMPLE_ON_RISING[(cpol, cpha)]
        self._word_size = word_size
        self._bit_order = bit_order
        self._clk_col = pin_map["clk"]
        self._mosi_col = pin_map["mosi"]
        self._miso_col: int | None = pin_map.get("miso")
        self._cs_col: int | None = pin_map.get("cs")
        # Idle assumptions for the very first sample of the stream.
        # CLK starts at its inactive level (matches the no-traffic idle).
        # CS starts inactive (HIGH).
        self._prev_clk = 1 if cpol == 1 else 0
        self._prev_cs = 1
        # In-progress word state.
        self._mosi_bits: list[int] = []
        self._miso_bits: list[int] = []
        self._word_index = 0
        # Position tracking.
        self._consumed_total = 0

    def feed(self, samples: np.ndarray) -> list[SpiTransaction]:
        n = len(samples)
        if n == 0:
            return []
        clk = samples[:, self._clk_col]
        mosi = samples[:, self._mosi_col]
        miso = samples[:, self._miso_col] if self._miso_col is not None else None
        cs = samples[:, self._cs_col] if self._cs_col is not None else None

        transactions: list[SpiTransaction] = []
        prev_clk = self._prev_clk
        prev_cs = self._prev_cs
        sample_on_rising = self._sample_on_rising
        word_size = self._word_size

        for i in range(n):
            curr_clk = int(clk[i])

            # CS deassertion mid-word check (only meaningful if a word is
            # in progress).
            if cs is not None and self._mosi_bits:
                prev_cs_active = prev_cs == 0
                curr_cs_active = cs[i] == 0
                if prev_cs_active and not curr_cs_active:
                    mosi_word, miso_word = _build_words(
                        self._mosi_bits, self._miso_bits,
                        word_size, self._bit_order, miso is not None,
                    )
                    transactions.append(SpiTransaction(
                        timestamp_s=(self._consumed_total + i) / self._sample_rate_hz,
                        word_index=self._word_index,
                        mosi=mosi_word,
                        miso=miso_word,
                        cs_active=True,
                        cs_error=True,
                        error=True,
                        error_detail="CS deasserted mid-word",
                    ))
                    self._word_index += 1
                    self._mosi_bits.clear()
                    self._miso_bits.clear()
                    prev_clk = curr_clk
                    prev_cs = int(cs[i])
                    continue

            rising = prev_clk == 0 and curr_clk == 1
            falling = prev_clk == 1 and curr_clk == 0
            if (sample_on_rising and rising) or (not sample_on_rising and falling):
                cs_active = bool(cs[i] == 0) if cs is not None else True
                if cs_active or cs is None:
                    self._mosi_bits.append(int(mosi[i]))
                    if miso is not None:
                        self._miso_bits.append(int(miso[i]))

                    if len(self._mosi_bits) == word_size:
                        mosi_word, miso_word = _build_words(
                            self._mosi_bits, self._miso_bits,
                            word_size, self._bit_order, miso is not None,
                        )
                        transactions.append(SpiTransaction(
                            timestamp_s=(self._consumed_total + i) / self._sample_rate_hz,
                            word_index=self._word_index,
                            mosi=mosi_word,
                            miso=miso_word,
                            cs_active=cs_active,
                            cs_error=False,
                        ))
                        self._word_index += 1
                        self._mosi_bits.clear()
                        self._miso_bits.clear()

            prev_clk = curr_clk
            if cs is not None:
                prev_cs = int(cs[i])

        self._prev_clk = prev_clk
        self._prev_cs = prev_cs
        self._consumed_total += n
        return transactions

    def finalize(self) -> list[SpiTransaction]:
        # Drop any partial word at end of stream. SPI words are atomic; a
        # truncated word would mislead callers more than help.
        self._mosi_bits.clear()
        self._miso_bits.clear()
        return []


def _build_words(
    mosi_bits: list[int],
    miso_bits: list[int],
    word_size: int,
    bit_order: str,
    has_miso: bool,
) -> tuple[bytes, bytes | None]:
    mosi_val = _bits_to_int(mosi_bits, word_size, bit_order)
    miso_val = _bits_to_int(miso_bits, word_size, bit_order) if has_miso and miso_bits else None
    return bytes([mosi_val]), bytes([miso_val]) if miso_val is not None else None


def _bits_to_int(bits: list[int], word_size: int, bit_order: str) -> int:
    val = 0
    if bit_order == "msb":
        for b in bits:
            val = (val << 1) | b
    else:
        for j, b in enumerate(bits):
            val |= b << j
    return val & ((1 << word_size) - 1)
