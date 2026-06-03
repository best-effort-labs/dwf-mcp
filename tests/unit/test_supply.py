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
