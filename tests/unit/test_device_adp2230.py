from __future__ import annotations

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DeviceInfo
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.policy import SafetyPolicy


def _adp2230_device(tmp_path) -> DwfDevice:
    # DeviceInfo is a frozen dataclass — build it directly with the drive/pull
    # caps that real hardware probes at open, so the capability-gated paths light up.
    info = DeviceInfo(
        serial="FAKE-ADP-0001", model="Analog Discovery Pro 2230", firmware="fake-1.0",
        sample_rate_max_hz=100_000_000.0, dio_count=16,
        analog_in_channels=2, analog_out_channels=3, devid=14,
        dio_pull_supported=True, dio_drive_supported=True,
        dio_drive_amp_min=0.004, dio_drive_amp_max=0.016,
        dio_drive_amp_steps=4, dio_drive_slew_steps=2,
    )
    dev = DwfDevice(
        backend=FakeBackend(devices=[info]),
        policy=SafetyPolicy(supply_max_voltage_pos=5.0, supply_max_voltage_neg=-5.0,
                            supply_max_current=1.0, awg_max_amplitude=5.0),
        allocator=PinAllocator(),
        workspace=tmp_path, idle_timeout_s=60,
    )
    dev.open()
    return dev


def test_adp2230_opens_with_profile_and_inventory(tmp_path):
    dev = _adp2230_device(tmp_path)
    try:
        assert dev.profile is not None and dev.profile.devid == 14
        assert dev.inventory is not None
        assert dev.inventory.awg_pins == ["awg1"]
        assert dev.inventory.scope_pins == ["scope1", "scope2"]
        assert "dio0" in dev.inventory.dio_pins and "dio15" in dev.inventory.dio_pins
    finally:
        dev.close()


def test_adp2230_single_awg_rejects_channel_two(tmp_path):
    dev = _adp2230_device(tmp_path)
    try:
        dev.validate_channel(1, "awg")  # channel 1 is valid
        with pytest.raises(ValueError, match="out of range 1..1"):
            dev.validate_channel(2, "awg")
    finally:
        dev.close()


def test_adp2230_drive_and_pull_caps_surface(tmp_path):
    dev = _adp2230_device(tmp_path)
    try:
        assert dev._info is not None
        assert dev._info.dio_drive_supported is True
        assert dev._info.dio_pull_supported is True
    finally:
        dev.close()
