from __future__ import annotations

from dwf_mcp.backend import DeviceInfo
from dwf_mcp.devices.inventory import build_inventory
from dwf_mcp.devices.profiles import resolve_profile


def _info(devid=10, dio=16, ain=2, aout=4):
    return DeviceInfo(
        serial="X", model="m", firmware="", devid=devid,
        sample_rate_max_hz=100_000_000.0, dio_count=dio,
        analog_in_channels=ain, analog_out_channels=aout,
        analog_in_buffer_max=16384, digital_in_buffer_max=16384,
        digital_word_width=16,
    )


def test_inventory_pin_namespace() -> None:
    inv = build_inventory(resolve_profile(10), _info())
    assert inv.dio_pins == [f"dio{i}" for i in range(16)]
    assert inv.scope_pins == ["scope1", "scope2"]
    assert inv.awg_pins == ["awg1", "awg2"]  # user count, not raw AnalogOut=4
    assert inv.supply_pins == ["vpos", "vneg"]
    assert "digital_in" in inv.virtual_resources
    assert inv.is_valid_pin("dio15") and not inv.is_valid_pin("dio16")
    assert inv.is_valid_pin("awg1") and not inv.is_valid_pin("awg3")
    assert inv.is_valid_pin("digital_in")


def test_inventory_scales_with_dio_count() -> None:
    inv = build_inventory(resolve_profile(10), _info(dio=24))
    assert inv.is_valid_pin("dio23") and not inv.is_valid_pin("dio24")
