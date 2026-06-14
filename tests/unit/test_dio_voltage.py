"""Tests for DIO.set_voltage — voltage-state model + policy enforcement."""
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend, make_dd_device
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.dio import DIO
from dwf_mcp.policy import SafetyPolicy

# --- Fixtures: Digital Discovery (adjustable-voltage device) ---

@pytest.fixture
def dd_device(tmp_path: Path) -> DwfDevice:
    d = DwfDevice(
        backend=FakeBackend(devices=[make_dd_device()]),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    d.open(serial="DD-0001")
    return d


@pytest.fixture
def dd_dio(dd_device: DwfDevice, tmp_path: Path) -> DIO:
    return DIO(device=dd_device, artifacts=ArtifactWriter(workspace=tmp_path))


# --- Fixtures: Classic AD3 (fixed 3.3 V DIO) ---

@pytest.fixture
def classic_device(tmp_path: Path) -> DwfDevice:
    d = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    d.open()
    return d


@pytest.fixture
def classic_dio(classic_device: DwfDevice, tmp_path: Path) -> DIO:
    return DIO(device=classic_device, artifacts=ArtifactWriter(workspace=tmp_path))


# --- Tests ---

def test_set_voltage_in_range_updates_state(dd_device: DwfDevice, dd_dio: DIO) -> None:
    out = dd_dio.set_voltage(1.8)
    assert out["voltage"] == 1.8
    assert dd_device.current_dio_voltage == 1.8


def test_set_voltage_out_of_range_rejected(dd_dio: DIO) -> None:
    with pytest.raises(ValueError, match="1.2.*3.3"):
        dd_dio.set_voltage(5.0)


def test_set_voltage_rejected_on_fixed_classic(classic_dio: DIO) -> None:
    with pytest.raises(ValueError, match="fixed|not adjustable"):
        classic_dio.set_voltage(2.5)


def test_set_voltage_at_boundary_accepted(dd_device: DwfDevice, dd_dio: DIO) -> None:
    """Boundary values (lo and hi) should be accepted."""
    dd_dio.set_voltage(1.2)
    assert dd_device.current_dio_voltage == 1.2
    dd_dio.set_voltage(3.3)
    assert dd_device.current_dio_voltage == 3.3


def test_set_voltage_calls_backend(dd_device: DwfDevice, dd_dio: DIO) -> None:
    dd_dio.set_voltage(1.8)
    fake: FakeBackend = dd_device.backend  # type: ignore[assignment]
    assert fake._dio_voltage == 1.8


def test_set_voltage_below_range_rejected(dd_dio: DIO) -> None:
    with pytest.raises(ValueError, match="1.2.*3.3"):
        dd_dio.set_voltage(0.5)


def test_set_voltage_policy_cap_enforced(tmp_path: Path) -> None:
    """Policy supply_max_voltage_pos=1.8 should block setting 3.3 V DIO voltage."""
    from dwf_mcp.policy import SafetyViolation
    d = DwfDevice(
        backend=FakeBackend(devices=[make_dd_device()]),
        policy=SafetyPolicy(supply_max_voltage_pos=1.8),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    d.open(serial="DD-0001")
    dio = DIO(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    with pytest.raises(SafetyViolation, match="1.8"):
        dio.set_voltage(3.3)
