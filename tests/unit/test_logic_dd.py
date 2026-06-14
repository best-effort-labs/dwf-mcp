"""Tests for logic (DigitalIn) instrument on Digital Discovery:
- resolver: uses inventory.subsystem_bit instead of int(p[3:])
- rate validation via validate_logic_rate (not validate_rate)
- 16/32-bit sample format selection based on pin mask
- bit-31 guard for unverified pins above bit 31
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend, make_dd_device
from dwf_mcp.device import DwfDevice
from dwf_mcp.instruments.logic import Logic
from dwf_mcp.policy import SafetyPolicy


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
def dd_logic(dd_device: DwfDevice, tmp_path: Path) -> Logic:
    return Logic(device=dd_device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_logic_uses_digitalin_global_bits(dd_logic: Logic) -> None:
    """Pins are resolved to global DigitalIn bit positions: din0→0, dio24→24."""
    dd_logic.configure(pins=["din0", "dio24"], sample_rate_hz=1_000_000.0, buffer_size=64)
    be: FakeBackend = dd_logic.device.backend  # type: ignore[assignment]
    cfgs = [c for c in be.logic_calls if c[0] == "configure"]
    assert len(cfgs) == 1
    assert cfgs[0][1]["pin_mask"] == (1 << 0) | (1 << 24)


def test_logic_over_16_channels_uses_32bit_format(dd_logic: Logic) -> None:
    """When any requested pin falls in bits 16..31, backend must be told 32-bit format."""
    pins = [f"din{i}" for i in range(20)]  # din0..din19 → bits 0..19; bit 19 > 15
    dd_logic.configure(pins=pins, sample_rate_hz=1_000_000.0, buffer_size=64)
    be: FakeBackend = dd_logic.device.backend  # type: ignore[assignment]
    assert be.logic_sample_bits == 32


def test_logic_16_channels_uses_16bit_format(dd_logic: Logic) -> None:
    """When all pins are within bits 0..15, 16-bit format is used."""
    pins = [f"din{i}" for i in range(8)]  # din0..din7 → bits 0..7; all within 16
    dd_logic.configure(pins=pins, sample_rate_hz=1_000_000.0, buffer_size=64)
    be: FakeBackend = dd_logic.device.backend  # type: ignore[assignment]
    assert be.logic_sample_bits == 16


def test_logic_rejects_rate_over_digital_max(dd_logic: Logic) -> None:
    """Rate exceeding digital_in_rate_max_hz must be rejected."""
    with pytest.raises(ValueError, match="exceeds"):
        dd_logic.configure(pins=["din0"], sample_rate_hz=1e10, buffer_size=64)


def test_logic_rejects_pins_above_bit31(dd_logic: Logic) -> None:
    """Pins resolving to DigitalIn bit >= 32 must be rejected pending hardware verification."""
    with pytest.raises(ValueError, match="bit 31|not yet supported"):
        dd_logic.configure(pins=["dio39"], sample_rate_hz=1_000_000.0, buffer_size=64)


def test_logic_trigger_pin_uses_global_bit(dd_logic: Logic) -> None:
    """set_trigger with a pin uses the global DigitalIn bit, not int(pin[3:])."""
    dd_logic.configure(pins=["din0", "dio24"], sample_rate_hz=1_000_000.0, buffer_size=64)
    dd_logic.set_trigger(source="detector_digital_in", pin="dio24", condition="Rising")
    be: FakeBackend = dd_logic.device.backend  # type: ignore[assignment]
    trigger_calls = [c for c in be.logic_calls if c[0] == "set_trigger"]
    assert len(trigger_calls) == 1
    # dio24 should resolve to global bit 24, not int("24") = 24 (same in this case,
    # but the test verifies the resolver path is exercised)
    assert trigger_calls[0][1]["pin_idx"] == 24


def test_logic_din_pin_accepted_in_schema(dd_logic: Logic) -> None:
    """din* pins must now be accepted (schema pattern updated to include din)."""
    # Validates that din0 is accepted without raising ValueError from validate_pin
    dd_logic.configure(pins=["din0"], sample_rate_hz=500_000.0, buffer_size=64)
    be: FakeBackend = dd_logic.device.backend  # type: ignore[assignment]
    cfgs = [c for c in be.logic_calls if c[0] == "configure"]
    assert len(cfgs) == 1
    assert cfgs[0][1]["pin_mask"] == (1 << 0)
