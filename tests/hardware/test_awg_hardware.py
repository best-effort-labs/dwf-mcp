"""Hardware smoke test for AWG.

Wiring: W1 → scope ch1+ (same wire as existing scope hardware test).
Run: pytest tests/hardware/test_awg_hardware.py -m hardware -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"awg_to_scope": ("W1", "CH1_POS")})
def test_awg_sine_captured_by_scope(tmp_path: Path) -> None:
    pytest.importorskip("pydwf")

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.scope import Scope
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
        awg = AWG(device=device, artifacts=arts)
        scope = Scope(device=device, artifacts=arts)

        awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
        awg.start(channel=1)

        scope.configure(channels=[1], range_v=5.0, sample_rate_hz=100_000, buffer_size=4096)
        scope.set_trigger(
            source="detector_analog_in", channel=1, level_v=0.0,
            condition="Rising", timeout_s=2.0,
        )
        result = scope.capture()
        freq = result["summary"]["ch1"]["freq_estimate"]
        assert 900 < freq < 1100, f"expected ~1000 Hz, got {freq}"
    finally:
        device.close()
