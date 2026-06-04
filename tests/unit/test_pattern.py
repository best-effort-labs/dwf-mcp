from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.pattern import Pattern
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(pattern_voltage="3.3"),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def pattern(device: DwfDevice, tmp_path: Path) -> Pattern:
    device.open()
    return Pattern(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pin(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    assert "dio0" in pattern.device.allocator.claimed_pins()


def test_configure_accumulates_pins(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.configure(pin="dio1", function="Clock", frequency_hz=500.0, duty=0.5, idle_state="low")
    pins = pattern.device.allocator.claimed_pins()
    assert "dio0" in pins and "dio1" in pins


def test_start_safety_gate_wrong_voltage_raises(tmp_path: Path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(pattern_voltage="5.0"),  # wrong voltage for AD3
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    p = Pattern(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
    p.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    with pytest.raises(SafetyViolation, match="3.3"):
        p.start(pin="dio0")


def test_start_calls_backend_start(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.start(pin="dio0")
    fake: FakeBackend = pattern.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.pattern_calls if c[0] == "start"]
    assert len(starts) == 1
    assert starts[0][1]["pin_idx"] == 0


def test_stop_does_not_release_claim(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.stop(pin="dio0")
    assert "dio0" in pattern.device.allocator.claimed_pins()


def test_release_clears_all_claims(pattern: Pattern) -> None:
    pattern.configure(pin="dio0", function="Pulse", frequency_hz=1000.0, duty=0.5, idle_state="low")
    pattern.configure(pin="dio1", function="Clock", frequency_hz=500.0, duty=0.5, idle_state="low")
    pattern.release()
    assert pattern.device.allocator.claimed_pins() == {}
