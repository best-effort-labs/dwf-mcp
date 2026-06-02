from __future__ import annotations

from dataclasses import dataclass


class SafetyViolation(Exception):
    """Raised when a tool call would exceed the active SafetyPolicy."""


@dataclass(frozen=True)
class SafetyPolicy:
    supply_max_voltage_pos: float = 3.3
    supply_max_voltage_neg: float = -3.3
    supply_max_current: float = 0.5
    awg_max_amplitude: float = 3.3
    pattern_voltage: str = "3.3"
    require_explicit_enable: bool = True

    def check_supply_voltage(self, channel: str, voltage: float) -> None:
        if channel == "pos" and voltage > self.supply_max_voltage_pos:
            raise SafetyViolation(
                f"supply.pos voltage {voltage} V exceeds policy cap "
                f"{self.supply_max_voltage_pos} V"
            )
        if channel == "neg" and voltage < self.supply_max_voltage_neg:
            raise SafetyViolation(
                f"supply.neg voltage {voltage} V exceeds policy cap "
                f"{self.supply_max_voltage_neg} V"
            )

    def check_supply_current(self, current: float) -> None:
        if current > self.supply_max_current:
            raise SafetyViolation(
                f"supply current {current} A exceeds policy cap "
                f"{self.supply_max_current} A"
            )

    def check_awg_amplitude(self, amplitude: float) -> None:
        if amplitude > self.awg_max_amplitude:
            raise SafetyViolation(
                f"AWG amplitude {amplitude} V exceeds policy cap "
                f"{self.awg_max_amplitude} V"
            )
