from __future__ import annotations

import time

import pytest


@pytest.mark.hardware
def test_supply_vpos_round_trip(tmp_path) -> None:
    """Enable vpos, read back, disable, verify the supply is released.

    Device-aware: a programmable supply (AD2/AD3) is driven to 1.0 V; the original
    Analog Discovery has a fixed +5 V rail, so on that device we set the rail's
    fixed voltage instead. Requires a classic Analog Discovery on USB; no external
    load required.

    The post-disable assertion intentionally does NOT check for voltage == 0: with
    no load on V+, the output capacitance holds residual charge for many seconds.
    The supply is genuinely disabled (state["enabled"] is False), but voltage decay
    is load-dependent. We assert the rail is no longer driven above the setpoint.
    """
    pytest.importorskip("pydwf")
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.instruments.supply import Supply
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        # Generous cap so both the 1.0 V (programmable) and 5.0 V (fixed) paths pass.
        policy=SafetyPolicy(supply_max_voltage_pos=5.0),
        allocator=PinAllocator(),  # configured from the profile at open
        workspace=tmp_path, idle_timeout_s=60,
    )
    device.open()
    try:
        fixed = device.profile.fixed_supply_voltages
        vset = fixed["vpos"] if (fixed and "vpos" in fixed) else 1.0
        lo, hi = vset - 0.3, vset + 0.3

        supply = Supply(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
        supply.set(channel="vpos", voltage=vset, current_limit=0.1)
        supply.enable(channel="vpos")
        # V+ ramps a few hundred ms from 0 to setpoint; 500 ms gives margin.
        time.sleep(0.5)
        state = supply.read(channel="vpos")
        assert state["enabled"] is True, state
        assert lo < state["measured"]["voltage"] < hi, state

        supply.disable(channel="vpos")
        time.sleep(0.2)
        state = supply.read(channel="vpos")
        assert state["enabled"] is False, state
        assert state["measured"]["voltage"] <= hi, state
    finally:
        device.close()
