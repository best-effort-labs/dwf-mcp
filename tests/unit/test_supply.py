from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.supply import Supply
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(
            supply_max_voltage_pos=3.3,
            supply_max_voltage_neg=-3.3,
            supply_max_current=0.5,
        ),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def supply(device: DwfDevice, tmp_path: Path) -> Supply:
    device.open()
    return Supply(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_set_stores_voltage_does_not_energize(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    fake = supply.device.backend  # type: ignore[assignment]
    # No master_enable call yet.
    enables = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    assert enables == []
    # Voltage and current_limit were written to the setpoint nodes.
    kinds = [c for c in fake.supply_calls if c[0] == "node_set"]  # type: ignore[attr-defined]
    assert any(c[1]["value"] == 3.0 for c in kinds)
    assert any(c[1]["value"] == 0.4 for c in kinds)


def test_set_claims_pin(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0)
    assert supply.device.allocator.claimed_pins() == {"vpos": "supply"}


def test_enable_above_cap_raises_safety_violation(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=5.0)  # set itself doesn't check
    with pytest.raises(SafetyViolation):
        supply.enable(channel="vpos")


def test_set_on_enabled_channel_above_cap_is_gated(supply: Supply) -> None:
    """Changing the setpoint of an already-energized rail writes live hardware,
    so it must route through the safety gate just like enable() does.

    Regression: previously set() never checked policy, letting a caller raise a
    live rail above the cap (e.g. enable at 3.0 V, then set to 5.0 V) with no gate.
    """
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    fake = supply.device.backend  # type: ignore[assignment]
    boundary = len(fake.supply_calls)  # type: ignore[attr-defined]

    with pytest.raises(SafetyViolation):
        supply.set(channel="vpos", voltage=5.0)

    # The over-cap voltage must never have been written to hardware.
    new_node_sets = [
        c for c in fake.supply_calls[boundary:] if c[0] == "node_set"  # type: ignore[attr-defined]
    ]
    assert all(c[1]["value"] != 5.0 for c in new_node_sets)
    # The stored setpoint must remain the last safe value.
    assert supply._setpoints["vpos"]["voltage"] == 3.0  # noqa: SLF001


def test_set_on_disabled_channel_above_cap_still_stages(supply: Supply) -> None:
    """A disabled channel is not live, so set() should keep staging without a gate
    (the gate fires at enable). Guards against over-gating the fix above."""
    supply.set(channel="vpos", voltage=5.0)  # no raise — channel not energized
    assert supply._setpoints["vpos"]["voltage"] == 5.0  # noqa: SLF001


def test_enable_within_cap_calls_master_enable(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    fake = supply.device.backend  # type: ignore[assignment]
    masters = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    assert masters[-1][1] == {"enabled": True}


def test_enable_without_set_raises_instrument_not_configured(supply: Supply) -> None:
    from dwf_mcp.instrument import InstrumentNotConfigured
    with pytest.raises(InstrumentNotConfigured):
        supply.enable(channel="vpos")


def test_disable_drops_master_when_no_rails_remain_on(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    supply.disable(channel="vpos")
    fake = supply.device.backend  # type: ignore[assignment]
    masters = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    # Sequence: True (on enable), False (on disable since no rails left).
    assert [m[1]["enabled"] for m in masters] == [True, False]


def test_disable_keeps_master_on_when_other_rail_still_on(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.set(channel="vneg", voltage=-3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    supply.enable(channel="vneg")
    supply.disable(channel="vpos")
    fake = supply.device.backend  # type: ignore[assignment]
    masters = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    # True, True, no second False.
    assert masters[-1][1]["enabled"] is True


def test_read_returns_requested_and_measured(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    # Override the measured voltage to simulate slight drift.
    layout = supply.device.backend.supply_discover_nodes()  # type: ignore[attr-defined]
    ch, nodes = layout["vpos"]
    supply.device.backend.set_supply_canned_status(  # type: ignore[attr-defined]
        {(ch, nodes["voltage"]): 2.97, (ch, nodes["current"]): 0.001}
    )
    state = supply.read(channel="vpos")
    assert state["requested"]["voltage"] == 3.0
    assert state["measured"]["voltage"] == 2.97
    assert state["measured"]["current"] == 0.001


def test_safety_log_records_supply_enable(supply: Supply, tmp_path: Path) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    import json
    lines = [
        json.loads(line)
        for line in (supply.device.workspace / "dwf-safety.log").read_text().splitlines()
        if line.strip()
    ]
    assert lines[-1]["kind"] == "supply_enable"
    assert lines[-1]["params"]["voltage"] == 3.0
    assert lines[-1]["rejected"] is False


def test_set_failure_releases_new_channel_when_no_prior(supply: Supply, monkeypatch) -> None:
    """Fresh set() on a new channel that fails mid-backend must drop the pin claim."""
    backend = supply.device.backend
    def boom_node_set(channel, node, value):
        raise RuntimeError("backend on fire")
    monkeypatch.setattr(backend, "supply_node_set", boom_node_set)
    with pytest.raises(RuntimeError):
        supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    # Pin claim must be released; no _setpoints entry.
    assert supply.device.allocator.claimed_pins() == {}
    assert "vpos" not in supply._setpoints  # noqa: SLF001


def test_set_failure_restores_prior_claims_and_setpoint(supply: Supply, monkeypatch) -> None:
    """A failed set() on an existing channel must preserve the prior claim AND setpoint.

    Sets up vpos with a known setpoint, then a failing set() on vneg. After the failure:
    - vpos should still be claimed (didn't lose ground)
    - vpos's setpoint should be unchanged
    - vneg should NOT be claimed (failure rolled back)
    - vneg should NOT have a setpoint
    """
    # Establish vpos as the prior state.
    supply.set(channel="vpos", voltage=2.5, current_limit=0.3)
    assert supply.device.allocator.claimed_pins() == {"vpos": "supply"}

    # Now make supply_node_set raise to break the next set() on vneg.
    backend = supply.device.backend
    original_node_set = backend.supply_node_set
    call_count = {"n": 0}
    def boom_on_second_call(channel, node, value):
        call_count["n"] += 1
        if call_count["n"] >= 1:  # fail on the very first call inside the new set()
            raise RuntimeError("backend on fire")
        original_node_set(channel, node, value)
    monkeypatch.setattr(backend, "supply_node_set", boom_on_second_call)

    with pytest.raises(RuntimeError):
        supply.set(channel="vneg", voltage=-3.0, current_limit=0.4)

    # vpos preserved.
    assert supply.device.allocator.claimed_pins() == {"vpos": "supply"}
    assert supply._setpoints.get("vpos") == {"voltage": 2.5, "current_limit": 0.3}  # noqa: SLF001
    # vneg rolled back.
    assert "vneg" not in supply._setpoints  # noqa: SLF001


def test_set_failure_restores_prior_setpoint_on_reset_of_same_channel(
    supply: Supply, monkeypatch
) -> None:
    """A failed re-set() of an existing channel must restore the original setpoint."""
    supply.set(channel="vpos", voltage=2.5, current_limit=0.3)

    backend = supply.device.backend
    def boom_node_set(channel, node, value):
        raise RuntimeError("backend on fire")
    monkeypatch.setattr(backend, "supply_node_set", boom_node_set)

    with pytest.raises(RuntimeError):
        supply.set(channel="vpos", voltage=5.0, current_limit=1.0)

    # Original setpoint preserved.
    assert supply._setpoints["vpos"] == {"voltage": 2.5, "current_limit": 0.3}  # noqa: SLF001
    # vpos still claimed (was claimed before, should stay claimed).
    assert supply.device.allocator.claimed_pins() == {"vpos": "supply"}


def test_fixed_supply_rejects_off_voltage_and_accepts_fixed(tmp_path: Path) -> None:
    """The original Analog Discovery (devid 2) has fixed +5/-5 V rails: set()
    must reject any other voltage and accept the fixed value. A programmable
    device (devid 10) accepts arbitrary values."""
    from dwf_mcp.backends.fake import make_fake_device

    ad1 = DwfDevice(
        backend=FakeBackend(devices=[make_fake_device(devid=2, model="Analog Discovery")]),
        policy=SafetyPolicy(supply_max_voltage_pos=5.0, supply_max_voltage_neg=-5.0,
                            supply_max_current=0.5),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path, idle_timeout_s=60,
    )
    ad1.open()
    sup = Supply(device=ad1, artifacts=ArtifactWriter(workspace=tmp_path))
    with pytest.raises(ValueError, match="fixed at 5.0 V"):
        sup.set(channel="vpos", voltage=1.0)
    sup.set(channel="vpos", voltage=5.0)  # the fixed value is accepted
    sup.set(channel="vneg", voltage=-5.0)
    with pytest.raises(ValueError, match="fixed at -5.0 V"):
        sup.set(channel="vneg", voltage=-3.0)
