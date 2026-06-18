from __future__ import annotations

import pytest

from dwf_mcp.devices.profiles import (
    PinBank,
    UnsupportedDeviceError,
    resolve_profile,
)


def test_resolve_classic_devids() -> None:
    for devid, name in [(2, "Analog Discovery"), (3, "Analog Discovery 2"),
                        (10, "Analog Discovery 3")]:
        p = resolve_profile(devid)
        assert p.devid == devid
        assert p.name == name
        assert p.user_awg_count == 2
        assert "scope" in p.supported_instruments
        assert p.dio_voltage_options == [3.3]


def test_unknown_devid_raises() -> None:
    with pytest.raises(UnsupportedDeviceError, match="devid 99"):
        resolve_profile(99)


def test_build_resource_groups_from_caps() -> None:
    p = resolve_profile(10)
    groups = p.build_resource_groups(analog_in_channels=2, user_awg_count=2)
    names = {g.name for g in groups}
    assert names == {"scope_pair", "awg_clock"}
    awg = next(g for g in groups if g.name == "awg_clock")
    assert awg.exclusive is True
    assert awg.pins == frozenset({"awg1", "awg2"})


def test_classic_profile_supports_all_registered_instruments(tmp_path) -> None:
    """The supported-instrument gate must not block any instrument the server
    actually registers (caught the 'decoder' omission). Guards against drift."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    registered = set(app.registry.names())
    supported = resolve_profile(10).supported_instruments
    missing = registered - supported
    assert missing == set(), f"classic profile missing registered instruments: {missing}"


def test_dd_profile_registered() -> None:
    p = resolve_profile(4)
    assert p.name == "Digital Discovery"
    assert p.user_awg_count == 0
    assert p.supported_instruments == frozenset({"dio", "logic", "pattern"})
    assert p.dio_voltage_range == (1.2, 3.3)
    assert p.pin_banks == [
        PinBank("din", 0, 24, input_only=True),
        PinBank("dio", 24, 16),
    ]


def test_classic_profiles_have_no_pin_banks_and_no_range() -> None:
    for devid in (2, 3, 10):
        p = resolve_profile(devid)
        assert p.pin_banks is None
        assert p.dio_voltage_range is None


def test_unsupported_devid_still_raises() -> None:
    with pytest.raises(UnsupportedDeviceError):
        resolve_profile(999)


def test_adp2230_profile_registered() -> None:
    from dwf_mcp.devices.profiles import _ALL_INSTRUMENTS
    p = resolve_profile(14)
    assert p.devid == 14
    assert p.name == "Analog Discovery Pro 2230"
    assert p.user_awg_count == 1                      # ONE AWG (W1); SDK reports 3
    assert p.supported_instruments == _ALL_INSTRUMENTS
    assert p.dio_voltage_options == [3.3]             # fixed 3.3 V LVCMOS
    assert p.fixed_supply_voltages is None            # programmable supplies
    assert p.pin_banks is None                        # single bidirectional bank
    assert p.dio_voltage_range is None                # no programmable DIO rail
