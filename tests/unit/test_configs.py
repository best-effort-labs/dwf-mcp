from __future__ import annotations

import pytest

from dwf_mcp.devices.configs import DeviceConfig, resolve_config_index

# Real config tables probed from hardware (index, DigIn, AnaIn, AnaOut, DigOut).
_AD2 = [
    DeviceConfig(0, 4096, 8192, 4096, 1024),
    DeviceConfig(1, 1024, 16384, 1024, 0),
    DeviceConfig(2, 0, 2048, 16384, 0),
    DeviceConfig(3, 16384, 512, 256, 16384),
    DeviceConfig(4, 4096, 8192, 4096, 1024),
    DeviceConfig(5, 2048, 8192, 4096, 256),
    DeviceConfig(6, 16384, 512, 256, 16384),
    DeviceConfig(7, 16384, 8192, 1024, 256),
]
_AD3 = [
    DeviceConfig(0, 16384, 16384, 16384, 2048),
    DeviceConfig(1, 4096, 32768, 4096, 2048),
    DeviceConfig(2, 2048, 8192, 32768, 2048),
    DeviceConfig(3, 32768, 16384, 4096, 2048),
    DeviceConfig(4, 32768, 4096, 4096, 32768),
    DeviceConfig(5, 2048, 8192, 16384, 2048),
]


def test_default_returns_none() -> None:
    assert resolve_config_index(_AD2, "default") is None
    assert resolve_config_index(_AD2, None) is None


def test_max_digital_in_ad2_picks_cfg7() -> None:
    # cfg3/cfg6 also have DigIn 16384, but cfg7 wins the AnaIn tiebreak (8192 vs 512).
    assert resolve_config_index(_AD2, "max_digital_in") == 7


def test_max_analog_in_ad2_picks_cfg1() -> None:
    assert resolve_config_index(_AD2, "max_analog_in") == 1


def test_max_digital_in_ad3_picks_cfg3() -> None:
    # DigIn 32768 in cfg3 and cfg4; cfg3 wins the AnaIn tiebreak (16384 vs 4096).
    assert resolve_config_index(_AD3, "max_digital_in") == 3


def test_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError, match="unknown device_config strategy"):
        resolve_config_index(_AD2, "max_everything")


def test_empty_config_table_returns_none() -> None:
    assert resolve_config_index([], "max_digital_in") is None
