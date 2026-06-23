"""Unit tests for the pure DigitalIn trigger helpers.

These cover the two bits of logic that the (previously broken) backend
`logic_set_trigger` got wrong: building the edge-detector bitmasks from a
pin + condition, and converting a position in seconds to an integer sample
count for `FDwfDigitalInTriggerPositionSet` (which takes samples, not seconds,
and rejects a float).
"""
from __future__ import annotations

import pytest

from dwf_mcp.logic_trigger import digital_trigger_masks, position_samples


class TestDigitalTriggerMasks:
    def test_rising_sets_only_edge_rise_bit(self) -> None:
        assert digital_trigger_masks(0, "Rising") == (0, 0, 0b1, 0)

    def test_falling_sets_only_edge_fall_bit(self) -> None:
        assert digital_trigger_masks(3, "Falling") == (0, 0, 0, 0b1000)

    def test_either_sets_both_edge_bits(self) -> None:
        assert digital_trigger_masks(2, "Either") == (0, 0, 0b100, 0b100)

    def test_no_pin_yields_empty_detector(self) -> None:
        assert digital_trigger_masks(None, "Rising") == (0, 0, 0, 0)

    def test_no_condition_yields_empty_detector(self) -> None:
        assert digital_trigger_masks(0, None) == (0, 0, 0, 0)

    def test_high_pin_index(self) -> None:
        assert digital_trigger_masks(15, "Rising") == (0, 0, 1 << 15, 0)

    def test_invalid_condition_raises(self) -> None:
        # A non-None but unrecognized condition is a programming error, not
        # "no detector" — surface it instead of silently arming nothing.
        with pytest.raises(ValueError, match="condition"):
            digital_trigger_masks(0, "Sideways")


class TestPositionSamples:
    def test_zero_seconds_is_zero_int(self) -> None:
        result = position_samples(0.0, 100_000_000.0)
        assert result == 0
        assert isinstance(result, int)

    def test_converts_seconds_to_samples(self) -> None:
        # 1 us at 100 MHz = 100 samples
        assert position_samples(1e-6, 100_000_000.0) == 100

    def test_rounds_to_nearest_sample(self) -> None:
        # 1.004 us at 100 MHz = 100.4 samples -> 100
        assert position_samples(1.004e-6, 100_000_000.0) == 100

    def test_always_returns_int_for_awkward_rate(self) -> None:
        result = position_samples(3.3e-6, 78_932_100.0)
        assert isinstance(result, int)

    def test_negative_position_clamps_to_zero(self) -> None:
        # The SDK takes an unsigned int; a negative count would wrap. Clamp.
        assert position_samples(-1e-6, 100_000_000.0) == 0
