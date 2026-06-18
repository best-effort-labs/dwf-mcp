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


def _dd_info():
    return DeviceInfo(serial="DD", model="Digital Discovery", firmware="x",
                      sample_rate_max_hz=0.0, dio_count=24, analog_in_channels=0,
                      analog_out_channels=0, devid=4)


def test_dd_inventory_native_names_and_input_only():
    inv = build_inventory(resolve_profile(4), _dd_info())
    assert "din0" in inv.dio_pins and "din23" in inv.dio_pins
    assert "dio24" in inv.dio_pins and "dio39" in inv.dio_pins
    assert "din0" in inv.input_only and "dio24" not in inv.input_only


def test_dd_subsystem_bit_mapping():
    inv = build_inventory(resolve_profile(4), _dd_info())
    assert inv.subsystem_bit("dio24", "digitalio") == 0
    assert inv.subsystem_bit("dio39", "digitalout") == 15
    assert inv.subsystem_bit("din5", "digitalin") == 5
    assert inv.subsystem_bit("dio24", "digitalin") == 24


def _adp2230_info():
    return DeviceInfo(
        serial="ADP", model="Analog Discovery Pro 2230", firmware="x",
        sample_rate_max_hz=100_000_000.0, dio_count=16,
        analog_in_channels=2, analog_out_channels=3,  # raw SDK count = 3
        devid=14,
        dio_pull_supported=True, dio_drive_supported=True,
        dio_drive_amp_min=0.004, dio_drive_amp_max=0.016,
        dio_drive_amp_steps=4, dio_drive_slew_steps=2,
    )


def test_adp2230_inventory_single_awg_namespace():
    inv = build_inventory(resolve_profile(14), _adp2230_info())
    assert inv.dio_pins == [f"dio{i}" for i in range(16)]
    assert inv.scope_pins == ["scope1", "scope2"]
    # ONE user AWG even though raw AnalogOut count is 3
    assert inv.awg_pins == ["awg1"]
    assert inv.is_valid_pin("awg1") and not inv.is_valid_pin("awg2")
    assert inv.supply_pins == ["vpos", "vneg"]
    assert inv.input_only == frozenset()           # bidirectional bank, no input-only
    assert inv.subsystem_bit("dio5", "digitalio") == 5
    assert inv.subsystem_bit("dio5", "digitalin") == 5


def test_classic_inventory_unchanged():
    info = DeviceInfo(serial="AD3", model="Analog Discovery 3", firmware="x",
                      sample_rate_max_hz=1e8, dio_count=16, analog_in_channels=2,
                      analog_out_channels=2, devid=10)
    inv = build_inventory(resolve_profile(10), info)
    assert inv.dio_pins == [f"dio{i}" for i in range(16)]
    assert inv.input_only == frozenset()
    assert inv.subsystem_bit("dio5", "digitalio") == 5
    assert inv.subsystem_bit("dio5", "digitalin") == 5
