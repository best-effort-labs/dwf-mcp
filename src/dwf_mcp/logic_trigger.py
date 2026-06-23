"""Pure helpers for configuring the DigitalIn (logic) trigger detector.

Kept free of any hardware/ctypes dependency so the mapping logic can be
unit-tested without a device. The backend wires the results into
`FDwfDigitalInTriggerSet` / `FDwfDigitalInTriggerPositionSet`.
"""
from __future__ import annotations


def digital_trigger_masks(
    pin_idx: int | None, condition: str | None
) -> tuple[int, int, int, int]:
    """Build the four DigitalIn detector bitmasks (level_low, level_high,
    edge_rise, edge_fall) for a single-pin edge trigger.

    `condition` is one of "Rising", "Falling", "Either" (the logic trigger
    schema is edge-only — there is no level condition). If either `pin_idx`
    or `condition` is missing, all masks are zero (no detector armed).
    """
    if pin_idx is None or condition is None:
        return (0, 0, 0, 0)
    if condition not in ("Rising", "Falling", "Either"):
        raise ValueError(
            f"condition must be one of Rising/Falling/Either, got {condition!r}"
        )
    bit = 1 << pin_idx
    edge_rise = bit if condition in ("Rising", "Either") else 0
    edge_fall = bit if condition in ("Falling", "Either") else 0
    return (0, 0, edge_rise, edge_fall)


def position_samples(position_s: float, sample_rate_hz: float) -> int:
    """Convert a trigger position in seconds to an integer sample count.

    `FDwfDigitalInTriggerPositionSet` takes the number of samples to acquire
    after the trigger as an unsigned int; passing a float raises a ctypes
    TypeError, so the conversion must round to an int here. A negative result
    is clamped to 0 (the SDK arg is unsigned and would otherwise wrap).
    """
    return max(0, int(round(position_s * sample_rate_hz)))
