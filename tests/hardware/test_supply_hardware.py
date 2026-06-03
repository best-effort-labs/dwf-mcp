from __future__ import annotations

import time

import pytest


@pytest.mark.hardware
def test_supply_vpos_round_trip(tmp_path) -> None:
    """Enable vpos at 1.0 V, read back, disable, read back ~0 V. Requires AD3."""
    pytest.importorskip("pydwf")
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.supply import Supply
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path, idle_timeout_s=60,
    )
    device.open()
    try:
        supply = Supply(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
        supply.set(channel="vpos", voltage=1.0, current_limit=0.1)
        supply.enable(channel="vpos")
        time.sleep(0.2)  # let the rail settle
        state = supply.read(channel="vpos")
        assert 0.9 < state["measured"]["voltage"] < 1.1, state
        supply.disable(channel="vpos")
        time.sleep(0.2)
        state = supply.read(channel="vpos")
        assert state["measured"]["voltage"] < 0.2, state
    finally:
        device.close()
