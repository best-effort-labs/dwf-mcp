"""Inventory capability-gating: supply pins only exist when the device supports supply,
and is_valid_physical_pin excludes virtual resources."""
from __future__ import annotations

from dwf_mcp.backend import DeviceInfo
from dwf_mcp.devices.inventory import build_inventory
from dwf_mcp.devices.profiles import resolve_profile


def _info(devid: int, *, dio: int, ain: int) -> DeviceInfo:
    return DeviceInfo(serial="X", model="m", firmware="", devid=devid,
                      sample_rate_max_hz=1, dio_count=dio,
                      analog_in_channels=ain, analog_out_channels=0)


def test_dd_has_no_supply_pins():
    inv = build_inventory(resolve_profile(4), _info(4, dio=40, ain=0))
    assert inv.supply_pins == []
    assert not inv.is_valid_physical_pin("vpos")
    assert not inv.is_valid_physical_pin("vneg")


def test_ad3_has_supply_pins():
    inv = build_inventory(resolve_profile(10), _info(10, dio=16, ain=2))
    assert inv.supply_pins == ["vpos", "vneg"]
    assert inv.is_valid_physical_pin("vpos")


def test_is_valid_physical_pin_excludes_virtual_resources():
    inv = build_inventory(resolve_profile(10), _info(10, dio=16, ain=2))
    assert inv.is_valid_physical_pin("dio0")
    assert inv.is_valid_physical_pin("scope1")
    assert not inv.is_valid_physical_pin("i2c_engine")   # virtual resource, not a pin
    assert not inv.is_valid_physical_pin("digital_in")
