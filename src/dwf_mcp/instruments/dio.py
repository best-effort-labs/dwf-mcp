"""DIO (DigitalIO) instrument. Transient per-call pin claim model."""
from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

_VALID_DIRECTIONS = frozenset({"in", "out"})

DIO_DIRECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin", "direction"],
    "properties": {
        "pin": {"type": "string", "pattern": "^(din|dio)\\d+$"},
        "direction": {"type": "string", "enum": ["in", "out"]},
    },
}

DIO_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin", "state"],
    "properties": {
        "pin": {"type": "string", "pattern": "^(din|dio)\\d+$"},
        "state": {"type": "integer", "enum": [0, 1]},
    },
}

DIO_PIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin"],
    "properties": {"pin": {"type": "string", "pattern": "^(din|dio)\\d+$"}},
}


class DIO(Instrument):
    name = "dio"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "set_direction": ("set_direction", DIO_DIRECTION_SCHEMA),
        "set":           ("set",           DIO_SET_SCHEMA),
        "read":          ("read",          DIO_PIN_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._directions: dict[str, str] = {}  # default "in" if not set

    def set_direction(self, pin: str, direction: str) -> dict[str, Any]:
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        if direction == "out":
            self.device.validate_output_pin(pin)
        else:
            self.device.validate_pin(pin)
        self._directions[pin] = direction
        return {"pin": pin, "direction": direction}

    def set(self, pin: str, state: int) -> dict[str, Any]:
        self.device.validate_output_pin(pin)
        direction = self._directions.get(pin, "in")
        if direction != "out":
            raise ValueError(
                f"pin {pin!r} direction is {direction!r}; call set_direction(pin, 'out') first"
            )
        assert self.device.inventory is not None  # guaranteed by validate_output_pin
        bit = self.device.inventory.subsystem_bit(pin, "digitalio")
        self.device.allocator.claim("dio", [pin])
        try:
            # Driving a pin high enables a hardware output — route through the
            # safety gate (logs to dwf-safety.log; enforces the fixed-3.3 V policy)
            # before touching hardware. The finally below releases the claim if the
            # gate rejects.
            self.device.gate_output("dio_set", pin=pin, state=int(state))
            self.device.backend.dio_set_direction(bit_idx=bit, output=True)
            self.device.backend.dio_set(bit_idx=bit, state=bool(state))
        finally:
            self.device.allocator.release("dio")
        return {"pin": pin, "state": state}

    def read(self, pin: str) -> dict[str, Any]:
        self.device.validate_pin(pin)
        assert self.device.inventory is not None  # guaranteed by validate_pin
        bit = self.device.inventory.subsystem_bit(pin, "digitalio")
        self.device.allocator.claim("dio", [pin])
        try:
            direction = self._directions.get(pin, "in")
            self.device.backend.dio_set_direction(bit_idx=bit, output=False)
            value = self.device.backend.dio_read(bit_idx=bit)
        finally:
            self.device.allocator.release("dio")
        return {"pin": pin, "state": int(value), "direction": direction}

    def release(self) -> None:
        self.device.allocator.release("dio")
        self._directions.clear()
