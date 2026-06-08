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
    (0, 1): True,   # fixed: CPOL=0 → active HIGH → rising edge
    (1, 0): False,  # fixed: CPOL=1 → active LOW  → falling edge
    (1, 1): False,
}


class SpiDecoder(Decoder):
    protocol_name: ClassVar[str] = "spi"

    def decode(  # type: ignore[override]
        self,
        samples: np.ndarray,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        mode: int = 0,
        bit_order: str = "msb",
        word_size: int = 8,
        **_: Any,
    ) -> list[SpiTransaction]:
        if mode not in (0, 1, 2, 3):
            raise ValueError(f"mode must be 0-3, got {mode!r}")
        cpol = mode >> 1
        cpha = mode & 1
        sample_on_rising = _SAMPLE_ON_RISING[(cpol, cpha)]

        clk = samples[:, pin_map["clk"]]
        mosi = samples[:, pin_map["mosi"]]
        miso_col = pin_map.get("miso")
        cs_col = pin_map.get("cs")
        miso = samples[:, miso_col] if miso_col is not None else None
        cs = samples[:, cs_col] if cs_col is not None else None

        transactions: list[SpiTransaction] = []
        mosi_bits: list[int] = []
        miso_bits: list[int] = []
        word_index = 0

        n = len(samples)
        for i in range(1, n):
            prev_clk = int(clk[i - 1])
            curr_clk = int(clk[i])

            # CS deassertion mid-word check
            if cs is not None and mosi_bits:
                prev_cs_active = cs[i - 1] == 0
                curr_cs_active = cs[i] == 0
                if prev_cs_active and not curr_cs_active:
                    mosi_word, miso_word = _build_words(
                        mosi_bits, miso_bits, word_size, bit_order, miso is not None
                    )
                    transactions.append(SpiTransaction(
                        timestamp_s=i / sample_rate_hz,
                        word_index=word_index,
                        mosi=mosi_word,
                        miso=miso_word,
                        cs_active=True,
                        cs_error=True,
                        error=True,
                        error_detail="CS deasserted mid-word",
                    ))
                    word_index += 1
                    mosi_bits.clear()
                    miso_bits.clear()
                    continue

            # CLK edge
            rising = prev_clk == 0 and curr_clk == 1
            falling = prev_clk == 1 and curr_clk == 0
            if (sample_on_rising and rising) or (not sample_on_rising and falling):
                cs_active = bool(cs[i] == 0) if cs is not None else True
                if not cs_active and cs is not None:
                    continue
                mosi_bits.append(int(mosi[i]))
                if miso is not None:
                    miso_bits.append(int(miso[i]))

                if len(mosi_bits) == word_size:
                    mosi_word, miso_word = _build_words(
                        mosi_bits, miso_bits, word_size, bit_order, miso is not None
                    )
                    transactions.append(SpiTransaction(
                        timestamp_s=i / sample_rate_hz,
                        word_index=word_index,
                        mosi=mosi_word,
                        miso=miso_word,
                        cs_active=cs_active,
                        cs_error=False,
                    ))
                    word_index += 1
                    mosi_bits.clear()
                    miso_bits.clear()

        return transactions


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
