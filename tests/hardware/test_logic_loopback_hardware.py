"""Portable hardware smoke test for Logic + Pattern: runs on any device with a
digital_loopback descriptor (AD3 dio0->dio1, DD dio24->dio25). On the DD this exercises
the 32-bit/bit-25 sample-format path; on the AD3 the 16-bit path.

Run: DWF_TEST_SERIAL=<serial> pytest tests/hardware/test_logic_loopback_hardware.py -m hardware -v
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"logic", "pattern"})
def test_pattern_clock_captured_by_logic(device, artifacts, digital_loopback) -> None:
    from dwf_mcp.instruments.logic import Logic
    from dwf_mcp.instruments.pattern import Pattern

    out_pin, in_pin = digital_loopback
    pat = Pattern(device=device, artifacts=artifacts)
    logic = Logic(device=device, artifacts=artifacts)

    pat.configure(pin=out_pin, function="Clock", frequency_hz=10_000.0,
                  duty=0.5, idle_state="low")
    try:
        pat.start(pin=out_pin)
        # 1 MHz vs 10 kHz over 4096 samples => ~40 cycles, ~80 edges.
        logic.configure(pins=[in_pin], sample_rate_hz=1_000_000, buffer_size=4096)
        result = logic.capture()
        assert "path" in result
        data = np.load(result["path"])[in_pin]
        assert 1 in data and 0 in data, "expected clock transitions on the input pin"
        edges = int(np.count_nonzero(np.diff(data.astype(int)) != 0))
        assert edges > 10, f"expected many clock edges, got {edges}"
    finally:
        pat.stop(pin=out_pin)
