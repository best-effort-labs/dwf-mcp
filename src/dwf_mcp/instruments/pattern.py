"""Pattern (DigitalOut) instrument. Per-pin accumulating claim model."""
from __future__ import annotations

import contextlib
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_Set = set

_VALID_FUNCTIONS = frozenset({"Pulse", "Clock", "Random", "Custom"})
_VALID_IDLE = frozenset({"low", "high", "hiz"})

PATTERN_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin", "function", "frequency_hz", "duty", "idle_state"],
    "properties": {
        "pin": {"type": "string", "pattern": "^dio\\d+$"},
        "function": {"type": "string", "enum": sorted(_VALID_FUNCTIONS)},
        "frequency_hz": {"type": "number", "minimum": 0.0},
        "duty": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "idle_state": {"type": "string", "enum": sorted(_VALID_IDLE)},
    },
}

PATTERN_PIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin"],
    "properties": {"pin": {"type": "string", "pattern": "^dio\\d+$"}},
}


def _pin_idx(pin: str) -> int:
    return int(pin[3:])


class Pattern(Instrument):
    name = "pattern"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", PATTERN_CONFIGURE_SCHEMA),
        "start":     ("start",     PATTERN_PIN_SCHEMA),
        "stop":      ("stop",      PATTERN_PIN_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured_pins: _Set[str] = set()

    def configure(
        self,
        pin: str,
        function: str,
        frequency_hz: float,
        duty: float,
        idle_state: str,
    ) -> dict[str, Any]:
        if function not in _VALID_FUNCTIONS:
            raise ValueError(f"function must be one of {sorted(_VALID_FUNCTIONS)}, got {function!r}")
        if idle_state not in _VALID_IDLE:
            raise ValueError(f"idle_state must be one of {sorted(_VALID_IDLE)}, got {idle_state!r}")
        prior_pins = _Set(self._configured_pins)
        new_pins = sorted(prior_pins | {pin})
        self.device.allocator.claim("pattern", new_pins)
        self._configured_pins.discard(pin)
        try:
            self.device.backend.pattern_configure(
                pin_idx=_pin_idx(pin),
                function=function,
                freq_hz=frequency_hz,
                duty=duty,
                idle_state=idle_state,
            )
        except Exception:
            if prior_pins:
                self.device.allocator.claim("pattern", sorted(prior_pins))
            else:
                self.device.allocator.release("pattern")
            self._configured_pins = prior_pins
            raise
        self._configured_pins.add(pin)
        return {"configured": True, "pin": pin}

    def start(self, pin: str) -> dict[str, Any]:
        self.device.validate_pin(pin)
        if pin not in self._configured_pins:
            raise InstrumentNotConfigured(
                f"pattern.configure must be called for {pin!r} before start"
            )
        self.device.gate_output("pattern_start", pin=pin, voltage=self.device.policy.pattern_voltage)
        self.device.backend.pattern_start(pin_idx=_pin_idx(pin))
        return {"started": True, "pin": pin}

    def stop(self, pin: str) -> dict[str, Any]:
        self.device.backend.pattern_stop(pin_idx=_pin_idx(pin))
        return {"stopped": True, "pin": pin}

    def release(self) -> None:
        for pin in list(self._configured_pins):
            with contextlib.suppress(Exception):
                self.device.backend.pattern_stop(pin_idx=_pin_idx(pin))
        self.device.allocator.release("pattern")
        self._configured_pins.clear()
