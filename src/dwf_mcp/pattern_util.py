"""Pure helper for sizing a DigitalOut (pattern) periodic waveform.

The DigitalOut per-phase counter is bounded (e.g. 32768 on the AD3), so a fixed
divider of 1 can only reach down to ~clock/(2*counter_max) Hz before the counts
overflow and the pin stops toggling. This picks the smallest divider that keeps
both phase counts within range, then sizes the counts against the divided clock.
"""
from __future__ import annotations

import math


def pattern_counts(
    clock_hz: float, freq_hz: float, duty: float, counter_max: int, divider_max: int
) -> tuple[int, int, int]:
    """Return (divider, low_count, high_count) for a Pulse/Clock waveform at
    `freq_hz` with `duty` (0..1), such that both counts fit within `counter_max`.

    For frequencies high enough that the counts already fit, the divider stays 1
    (so existing behaviour is unchanged). The achieved frequency is
    clock_hz / (divider * (low_count + high_count)).

    Each phase is at least one tick, so duty exactly 0.0/1.0 still toggles (a
    one-tick minor phase) rather than holding the line constant. Raises ValueError
    if `freq_hz` is so low that the required divider exceeds `divider_max`.
    """
    period_base = clock_hz / freq_hz
    # The longer of the two phases (worst case for fitting) is max(duty,1-duty).
    larger_frac = max(duty, 1.0 - duty)
    divider = max(1, math.ceil(period_base * larger_frac / counter_max))
    if divider > divider_max:
        raise ValueError(
            f"frequency {freq_hz} Hz is too low for the hardware "
            f"(needs divider {divider} > max {divider_max})"
        )
    period = max(1, round(period_base / divider))
    high_count = min(max(1, round(period * duty)), counter_max)
    low_count = min(max(1, period - high_count), counter_max)
    return divider, low_count, high_count
