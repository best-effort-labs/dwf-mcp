from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator, PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.dio import DIO
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def dio(device: DwfDevice, tmp_path: Path) -> DIO:
    device.open()
    return DIO(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_default_direction_is_in(dio: DIO) -> None:
    assert dio._directions.get("dio0", "in") == "in"


def test_set_direction_does_not_touch_hardware(dio: DIO) -> None:
    dio.set_direction(pin="dio0", direction="out")
    fake: FakeBackend = dio.device.backend  # type: ignore[assignment]
    assert fake.dio_calls == []


def test_set_on_in_pin_raises_before_claim(dio: DIO) -> None:
    # Default direction is "in"; set should raise ValueError before claiming.
    with pytest.raises(ValueError, match="direction"):
        dio.set(pin="dio0", state=1)
    assert dio.device.allocator.claimed_pins() == {}


def test_set_writes_hardware_and_releases_claim(dio: DIO) -> None:
    dio.set_direction(pin="dio0", direction="out")
    dio.set(pin="dio0", state=1)
    # Claim must be released after the call.
    assert dio.device.allocator.claimed_pins() == {}
    # Hardware was called.
    fake: FakeBackend = dio.device.backend  # type: ignore[assignment]
    direction_calls = [c for c in fake.dio_calls if c[0] == "set_direction"]
    set_calls = [c for c in fake.dio_calls if c[0] == "set"]
    assert len(direction_calls) == 1
    assert direction_calls[0][1]["output"] is True
    assert len(set_calls) == 1
    assert set_calls[0][1]["state"] is True


def test_set_records_safety_log_entry(dio: DIO) -> None:
    """Driving a DIO pin high enables a hardware output, so it must route through
    gate_output and be recorded in the safety log (the output-enabling invariant)."""
    import json
    dio.set_direction(pin="dio0", direction="out")
    dio.set(pin="dio0", state=1)
    lines = [
        json.loads(line)
        for line in (dio.device.workspace / "dwf-safety.log").read_text().splitlines()
        if line.strip()
    ]
    assert lines[-1]["kind"] == "dio_set"
    assert lines[-1]["rejected"] is False


def test_set_rejected_when_policy_voltage_unsatisfiable(tmp_path: Path) -> None:
    """DIO output is fixed 3.3 V hardware, same as the pattern generator. A policy
    whose voltage can't be met by hardware must reject dio.set, just like pattern."""
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(pattern_voltage="5.0"),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    dio = DIO(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
    dio.set_direction(pin="dio0", direction="out")
    with pytest.raises(SafetyViolation):
        dio.set(pin="dio0", state=1)
    # Rejected before any hardware write, and the claim is released.
    fake: FakeBackend = device.backend  # type: ignore[assignment]
    assert [c for c in fake.dio_calls if c[0] == "set"] == []
    assert device.allocator.claimed_pins() == {}


def test_read_releases_claim(dio: DIO) -> None:
    result = dio.read(pin="dio0")
    assert isinstance(result, dict)
    assert dio.device.allocator.claimed_pins() == {}


def test_set_raises_pin_allocation_error_if_held(dio: DIO) -> None:
    # Claim dio0 from outside.
    dio.device.allocator.claim("scope", ["dio0"])
    dio.set_direction(pin="dio0", direction="out")
    with pytest.raises(PinAllocationError):
        dio.set(pin="dio0", state=1)
