"""Hardware smoke test for Logic + Pattern on the Digital Discovery (devid 4).

Wiring (DD left side connector): DIO24 -> DIO25 loopback + JL GND -> DD GND.
Pattern drives a 10 kHz clock on DIO24; logic captures DIO25.
The JL-GND <-> DD-GND tie is mandatory: the Jumperless CH446Q crosspoints are analog
switches and only pass a signal referenced within their supply rails.

Run: DWF_TEST_SERIAL=210321AD4ECF pytest tests/hardware/test_logic_dd_hardware.py -m hardware -v
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.device(devid=4)
@pytest.mark.jumperless(connections={
    "loopback": ("DIO24", "DIO25"),
    "gnd": ("DD_GND", "GND"),
})
def test_dd_pattern_clock_captured_by_logic(device, artifacts) -> None:
    from dwf_mcp.instruments.logic import Logic
    from dwf_mcp.instruments.pattern import Pattern

    pat = Pattern(device=device, artifacts=artifacts)
    logic = Logic(device=device, artifacts=artifacts)

    pat.configure(pin="dio24", function="Clock", frequency_hz=10_000.0,
                  duty=0.5, idle_state="low")
    try:
        pat.start(pin="dio24")
        # 1 MHz sample rate vs 10 kHz clock over 4096 samples => ~40 cycles, ~80 edges.
        logic.configure(pins=["dio25"], sample_rate_hz=1_000_000, buffer_size=4096)
        result = logic.capture()
        assert "path" in result
        dio25 = np.load(result["path"])["dio25"]
        assert 1 in dio25 and 0 in dio25, "expected clock transitions on DIO25"
        edges = int(np.count_nonzero(np.diff(dio25.astype(int)) != 0))
        assert edges > 10, f"expected many clock edges on DIO25, got {edges}"
    finally:
        pat.stop(pin="dio24")
