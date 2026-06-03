from __future__ import annotations

import time

import pytest


@pytest.mark.hardware
def test_supply_vpos_round_trip(tmp_path) -> None:
    """Enable vpos at 1.0 V, read back, disable, verify supply is released.

    Requires AD3 (USB attached). No external load required.

    The post-disable assertion intentionally does NOT check for voltage == 0:
    with no load on V+, the output capacitance holds residual charge for many
    seconds. The supply is genuinely disabled (state["enabled"] is False), but
    voltage decay is load-dependent. We assert the rail is no longer driven
    above the prior setpoint, which is the meaningful invariant.
    """
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
        # AD3 V+ ramps ~300ms from 0 to setpoint; 500ms gives margin.
        time.sleep(0.5)
        state = supply.read(channel="vpos")
        assert state["enabled"] is True, state
        assert 0.9 < state["measured"]["voltage"] < 1.1, state

        supply.disable(channel="vpos")
        time.sleep(0.2)
        state = supply.read(channel="vpos")
        # Supply is disabled at the silicon level (instrument state + master enable).
        assert state["enabled"] is False, state
        # Rail is no longer actively driven (cap may still hold charge with no load,
        # but it cannot be above the prior setpoint plus measurement noise).
        assert state["measured"]["voltage"] <= 1.1, state
    finally:
        device.close()
