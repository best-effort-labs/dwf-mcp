from __future__ import annotations

import pytest

from dwf_mcp.policy import SafetyPolicy, SafetyViolation


def test_default_policy_blocks_outputs() -> None:
    p = SafetyPolicy()
    assert p.require_explicit_enable is True


def test_supply_voltage_cap_enforced() -> None:
    p = SafetyPolicy(supply_max_voltage_pos=3.3)
    p.check_supply_voltage("pos", 3.3)  # boundary OK
    with pytest.raises(SafetyViolation) as exc:
        p.check_supply_voltage("pos", 3.31)
    assert "3.31" in str(exc.value)
    assert "3.3" in str(exc.value)


def test_supply_negative_cap_enforced() -> None:
    p = SafetyPolicy(supply_max_voltage_neg=-3.3)
    p.check_supply_voltage("neg", -3.3)
    with pytest.raises(SafetyViolation):
        p.check_supply_voltage("neg", -3.31)


def test_supply_current_cap_enforced() -> None:
    p = SafetyPolicy(supply_max_current=0.5)
    p.check_supply_current(0.5)
    with pytest.raises(SafetyViolation):
        p.check_supply_current(0.51)


def test_awg_amplitude_cap_enforced() -> None:
    p = SafetyPolicy(awg_max_amplitude=3.3)
    p.check_awg_amplitude(3.3)
    with pytest.raises(SafetyViolation):
        p.check_awg_amplitude(3.31)


def test_policy_is_frozen() -> None:
    p = SafetyPolicy(supply_max_voltage_pos=3.3)
    with pytest.raises((AttributeError, TypeError)):
        p.supply_max_voltage_pos = 5.0  # type: ignore[misc]
