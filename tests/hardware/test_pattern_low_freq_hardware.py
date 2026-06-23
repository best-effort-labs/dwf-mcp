"""Hardware validation that the pattern generator produces output at LOW
frequencies. Regression guard for the bug where `pattern_configure` hardcoded
divider=1, so below ~clock/(2*counter_max) Hz (~1.5 kHz on the AD3) the per-phase
counts overflowed the DigitalOut counter and the pin never toggled.

Self-wires a 1 kHz clock on out_pin -> in_pin via the digital_loopback descriptor,
captures it, and asserts both that it toggles and that the period is ~1 kHz.

Run: DWF_TEST_SERIAL=<serial> pytest tests/hardware/test_pattern_low_freq_hardware.py -m hardware -v
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"logic", "pattern"})
def test_pattern_one_khz_clock_toggles_at_correct_rate(
    device, artifacts, digital_loopback
) -> None:
    from dwf_mcp.instruments.logic import Logic
    from dwf_mcp.instruments.pattern import Pattern

    out_pin, in_pin = digital_loopback
    pat = Pattern(device=device, artifacts=artifacts)
    logic = Logic(device=device, artifacts=artifacts)

    freq_hz = 1_000.0
    sample_rate_hz = 100_000.0
    expected_period_samples = sample_rate_hz / freq_hz  # 100

    pat.configure(pin=out_pin, function="Clock", frequency_hz=freq_hz,
                  duty=0.5, idle_state="low")
    try:
        pat.start(pin=out_pin)
        # 1 kHz at 100 kHz sampling => 100 samples/period; 4096 samples ~ 40 cycles.
        logic.configure(pins=[in_pin], sample_rate_hz=sample_rate_hz, buffer_size=4096)
        data = np.load(logic.capture()["path"])[in_pin].astype(int)
    finally:
        pat.stop(pin=out_pin)

    rising = np.where(np.diff(data) == 1)[0]
    assert rising.size >= 10, (
        f"1 kHz clock did not toggle (only {rising.size} rising edges) -- "
        "pattern_configure produced no output at low frequency"
    )
    median_period = float(np.median(np.diff(rising)))
    assert abs(median_period - expected_period_samples) / expected_period_samples < 0.1, (
        f"measured period {median_period:.1f} samples != ~{expected_period_samples:.0f} (1 kHz)"
    )
