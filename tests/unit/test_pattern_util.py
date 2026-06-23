"""Unit tests for the DigitalOut pattern counts helper.

The bug this guards: `pattern_configure` hardcoded divider=1, so at low
frequencies the per-phase counter values exceeded the DigitalOut counter max
(32768 on the AD3) and the pin produced no output. The helper must raise the
divider so both counts always fit.
"""
from __future__ import annotations

import pytest

from dwf_mcp.pattern_util import pattern_counts

CLOCK = 100_000_000.0   # AD3 DigitalOut internal clock
CMAX = 32768            # AD3 counter max
DMAX = 2_147_483_649    # AD3 divider max


def achieved_hz(divider: int, low: int, high: int) -> float:
    return CLOCK / (divider * (low + high))


class TestPatternCounts:
    def test_high_freq_keeps_divider_one(self) -> None:
        # 1 MHz: period 100 ticks, easily fits -> divider stays 1 (no regression).
        divider, low, high = pattern_counts(CLOCK, 1_000_000.0, 0.5, CMAX, DMAX)
        assert divider == 1
        assert (low, high) == (50, 50)

    def test_ten_khz_unchanged(self) -> None:
        # 10 kHz is the frequency the existing hardware tests use; must be divider 1.
        divider, low, high = pattern_counts(CLOCK, 10_000.0, 0.5, CMAX, DMAX)
        assert divider == 1
        assert low + high == 10_000

    def test_one_khz_fits_counter_via_divider(self) -> None:
        # The bug case: at divider=1 each phase would be 50000 > 32768.
        divider, low, high = pattern_counts(CLOCK, 1_000.0, 0.5, CMAX, DMAX)
        assert divider > 1
        assert low <= CMAX and high <= CMAX
        assert achieved_hz(divider, low, high) == 1_000.0

    def test_very_low_freq_fits(self) -> None:
        divider, low, high = pattern_counts(CLOCK, 1.0, 0.5, CMAX, DMAX)
        assert low <= CMAX and high <= CMAX
        assert abs(achieved_hz(divider, low, high) - 1.0) / 1.0 < 0.01

    def test_asymmetric_duty_larger_phase_fits(self) -> None:
        # duty 0.9 at low freq: the long (high) phase must still fit.
        divider, low, high = pattern_counts(CLOCK, 1_000.0, 0.9, CMAX, DMAX)
        assert low <= CMAX and high <= CMAX
        assert high > low  # 90% duty -> high phase longer

    def test_counts_never_below_one(self) -> None:
        divider, low, high = pattern_counts(CLOCK, 1_000_000.0, 0.99, CMAX, DMAX)
        assert low >= 1 and high >= 1

    def test_asymmetric_duty_achieves_frequency(self) -> None:
        # Where rounding/clamping is most likely to drift: low freq + skewed duty.
        divider, low, high = pattern_counts(CLOCK, 1_000.0, 0.9, CMAX, DMAX)
        assert abs(achieved_hz(divider, low, high) - 1_000.0) / 1_000.0 < 0.01

    def test_unattainable_frequency_raises(self) -> None:
        # If the required divider exceeds the hardware max, the frequency is
        # unreachable -- signal it instead of silently clamping to a wrong rate.
        with pytest.raises(ValueError, match="frequency"):
            pattern_counts(CLOCK, 1.0, 0.5, CMAX, divider_max=10)
