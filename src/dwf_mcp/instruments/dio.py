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

DIO_VOLTAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["voltage"],
    "properties": {"voltage": {"type": "number"}},
}

DIO_PULL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pin", "mode"],
    "properties": {
        "pin": {"type": "string", "pattern": "^(din|dio)\\d+$"},
        "mode": {"type": "string", "enum": ["up", "down", "none", "keeper"]},
    },
}

DIO_DRIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["milliamps", "slew"],
    "properties": {
        "milliamps": {"type": "number"},
        "slew": {"type": "integer", "minimum": 0},
        "bank": {"type": "integer", "minimum": 0, "default": 0},
    },
}


class DIO(Instrument):
    name = "dio"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "set_direction": ("set_direction", DIO_DIRECTION_SCHEMA),
        "set":           ("set",           DIO_SET_SCHEMA),
        "read":          ("read",          DIO_PIN_SCHEMA),
        "set_voltage":   ("set_voltage",   DIO_VOLTAGE_SCHEMA),
        "set_pull":      ("set_pull",      DIO_PULL_SCHEMA),
        "set_drive":     ("set_drive",     DIO_DRIVE_SCHEMA),
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

    def set_voltage(self, voltage: float) -> dict[str, Any]:
        prof = self.device.profile
        rng = prof.dio_voltage_range if prof else None
        if rng is None:
            raise ValueError(
                f"DIO voltage is fixed (not adjustable) on {self.device._device_name()}"
            )
        lo, hi = rng
        if not (lo <= voltage <= hi):
            raise ValueError(f"voltage {voltage} V outside {lo}..{hi} V range")
        self.device.gate_output("dio_voltage", voltage=float(voltage))
        self.device.backend.dio_set_voltage(float(voltage))
        self.device.current_dio_voltage = float(voltage)
        return {"voltage": voltage}

    def set_pull(self, pin: str, mode: str) -> dict[str, Any]:
        self.device.validate_pin(pin)
        info = self.device._info
        if not (info and info.dio_pull_supported):
            raise ValueError(f"pull not supported on {self.device._device_name()}")
        if pin.startswith("din"):
            if mode == "keeper":
                # DIN pull is the analogIO DINPP scalar (down/none/up only) — no keeper.
                raise ValueError("keeper pull mode is not supported on the DIN bank (din* pins)")
            self.device.backend.din_pull_set(mode)
            return {"pin": pin, "mode": mode, "scope": "din_bank",
                    "note": "DIN pull is bank-global; affects all din pins"}
        assert self.device.inventory is not None  # guaranteed by validate_pin
        bit = self.device.inventory.subsystem_bit(pin, "digitalio")
        self.device.backend.dio_pull_set(bit_idx=bit, mode=mode)
        return {"pin": pin, "mode": mode, "scope": "pin"}

    def set_drive(self, milliamps: float, slew: int, bank: int = 0) -> dict[str, Any]:
        info = self.device._info
        if not (info and info.dio_drive_supported):
            raise ValueError(f"drive config not supported on {self.device._device_name()}")
        amps = milliamps / 1000.0
        if not (info.dio_drive_amp_min <= amps <= info.dio_drive_amp_max):
            raise ValueError(
                f"milliamps {milliamps} outside "
                f"{info.dio_drive_amp_min * 1000}..{info.dio_drive_amp_max * 1000} mA range")
        if not (0 <= slew < max(1, info.dio_drive_slew_steps)):
            raise ValueError(f"slew {slew} outside 0..{info.dio_drive_slew_steps - 1}")
        self.device.backend.dio_drive_set(bank=bank, amps=amps, slew=slew)
        return {"bank": bank, "milliamps": milliamps, "slew": slew}

    def release(self) -> None:
        self.device.allocator.release("dio")
        self._directions.clear()
