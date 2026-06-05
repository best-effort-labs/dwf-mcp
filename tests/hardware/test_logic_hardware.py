"""Hardware smoke test for Logic and Pattern.

Wiring: DIO0 → DIO1 loopback (pattern drives DIO0, logic captures DIO1).
Run: pytest tests/hardware/test_logic_hardware.py -m hardware -v
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_pattern_clock_captured_by_logic(tmp_path: Path) -> None:
    pytest.importorskip("pydwf")

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.logic import Logic
    from dwf_mcp.instruments.pattern import Pattern
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    try:
        arts = ArtifactWriter(workspace=tmp_path)
        pat = Pattern(device=device, artifacts=arts)
        logic = Logic(device=device, artifacts=arts)

        # Drive DIO0 at 10 kHz clock, capture DIO1 at 1 MHz.
        pat.configure(
            pin="dio0", function="Clock", frequency_hz=10_000.0, duty=0.5, idle_state="low"
        )
        pat.start(pin="dio0")

        logic.configure(pins=["dio1"], sample_rate_hz=1_000_000, buffer_size=4096)
        result = logic.capture()
        assert "path" in result
        loaded = np.load(result["path"])
        dio1 = loaded["dio1"]
        # At 1 MHz sample rate and 10 kHz clock, expect ~100 samples per period.
        # Check that dio1 has both 0 and 1 values (the clock is toggling).
        assert 1 in dio1 and 0 in dio1, "expected clock transitions on DIO1"
    finally:
        device.close()
