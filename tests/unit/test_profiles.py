from __future__ import annotations

import pytest

from dwf_mcp.devices.profiles import (
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
