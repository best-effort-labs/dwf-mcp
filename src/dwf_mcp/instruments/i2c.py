"""I2C active-master instrument. Wraps pydwf.ProtocolI2C via the DwfBackend seam."""
from __future__ import annotations

import re
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_DIO_PATTERN = re.compile(r"^dio(\d+)$")

I2C_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sda_pin", "scl_pin", "clock_hz"],
    "properties": {
        "sda_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "scl_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "clock_hz": {"type": "number", "minimum": 100, "maximum": 1_000_000},
        "pullups": {"type": "boolean", "default": False},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 0.1},
        "stretch": {"type": "boolean", "default": True},
    },
}

I2C_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["address", "data"],
    "properties": {
        "address": {"type": "integer", "minimum": 0, "maximum": 0x7F},
        "data": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 0xFF}},
    },
}

I2C_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["address", "length"],
    "properties": {
        "address": {"type": "integer", "minimum": 0, "maximum": 0x7F},
        "length": {"type": "integer", "minimum": 1, "maximum": 4096},
    },
}

I2C_WRITE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["address", "write", "read_length"],
    "properties": {
        "address": {"type": "integer", "minimum": 0, "maximum": 0x7F},
        "write": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 0xFF}},
        "read_length": {"type": "integer", "minimum": 0, "maximum": 4096},
    },
}

I2C_SCAN_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _dio_index(pin: str) -> int:
    m = _DIO_PATTERN.match(pin)
    if not m:
        raise ValueError(f"expected pin like 'dio0'..'dio15', got {pin!r}")
    return int(m.group(1))


def _to_bytes(data: list[int] | bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    return bytes(data)


class I2C(Instrument):
    name = "i2c"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":  ("configure",  I2C_CONFIGURE_SCHEMA),
        "write":      ("write",      I2C_WRITE_SCHEMA),
        "read":       ("read",       I2C_READ_SCHEMA),
        "write_read": ("write_read", I2C_WRITE_READ_SCHEMA),
        "scan":       ("scan",       I2C_SCAN_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False
        self._sda_pin: str | None = None
        self._scl_pin: str | None = None
        self._clock_hz: float = 0
        self._pullups: bool = False

    def configure(
        self,
        sda_pin: str,
        scl_pin: str,
        clock_hz: float,
        pullups: bool = False,
        timeout_s: float = 0.1,
        stretch: bool = True,
    ) -> dict[str, Any]:
        if sda_pin == scl_pin:
            raise ValueError("sda_pin and scl_pin must be different")
        sda_idx = _dio_index(sda_pin)
        scl_idx = _dio_index(scl_pin)
        # Partial-failure pattern: claim pins, clear stale state, try backend calls,
        # on failure release the I2C claim entirely (configure is fresh-state each call).
        self.device.allocator.claim("i2c", [sda_pin, scl_pin])
        self._configured = False
        self._sda_pin = None
        self._scl_pin = None
        try:
            self.device.backend.i2c_reset()
            self.device.backend.i2c_configure(
                scl_pin_idx=scl_idx, sda_pin_idx=sda_idx,
                rate_hz=clock_hz, stretch=stretch, timeout_s=timeout_s,
            )
        except Exception:
            self.device.allocator.release("i2c")
            raise
        self._configured = True
        self._sda_pin = sda_pin
        self._scl_pin = scl_pin
        self._clock_hz = clock_hz
        self._pullups = pullups
        return {"configured": True, "sda_pin": sda_pin, "scl_pin": scl_pin,
                "clock_hz": clock_hz, "pullups": pullups}

    def write(self, address: int, data: list[int] | bytes) -> dict[str, Any]:
        self._require_configured()
        nak = self.device.backend.i2c_write(address=address, data=_to_bytes(data))
        return {"address": address, "ack": nak == 0, "nak_count": nak}

    def read(self, address: int, length: int) -> dict[str, Any]:
        self._require_configured()
        data = self.device.backend.i2c_read(address=address, length=length)
        return {"address": address, "data_hex": data.hex(), "data": list(data)}

    def write_read(
        self, address: int, write: list[int] | bytes, read_length: int
    ) -> dict[str, Any]:
        self._require_configured()
        data = self.device.backend.i2c_write_read(
            address=address, write_data=_to_bytes(write), read_length=read_length,
        )
        return {"address": address, "data_hex": data.hex(), "data": list(data)}

    def scan(self) -> dict[str, Any]:
        self._require_configured()
        found: list[int] = []
        for addr in range(0x08, 0x78):
            nak = self.device.backend.i2c_write_one(address=addr, byte=0)
            if nak == 0:
                found.append(addr)
        return {"found": found, "count": len(found)}

    def release(self) -> None:
        self.device.allocator.release("i2c")
        self._configured = False
        self._sda_pin = None
        self._scl_pin = None

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("i2c.configure must be called before any I/O operation")
