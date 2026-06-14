from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.pin_utils import dio_index as _dio_index


SPI_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["clk_pin", "frequency_hz", "mode"],
    "properties": {
        "clk_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "frequency_hz": {"type": "number", "minimum": 1, "maximum": 50_000_000},
        "mode": {"type": "integer", "enum": [0, 1, 2, 3]},
        "mosi_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "miso_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "cs_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "cs_polarity": {
            "type": "string", "enum": ["active_low", "active_high"],
            "default": "active_low",
        },
        "bit_order": {"type": "string", "enum": ["msb", "lsb"], "default": "msb"},
    },
}

SPI_TRANSFER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["data"],
    "properties": {
        "data": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
        },
        "assert_cs": {"type": "boolean", "default": True},
    },
}

SPI_WRITE_SCHEMA = SPI_TRANSFER_SCHEMA

SPI_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["length"],
    "properties": {
        "length": {"type": "integer", "minimum": 1, "maximum": 65536},
        "assert_cs": {"type": "boolean", "default": True},
    },
}


class SPI(Instrument):
    name = "spi"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", SPI_CONFIGURE_SCHEMA),
        "transfer":  ("transfer",  SPI_TRANSFER_SCHEMA),
        "write":     ("write",     SPI_WRITE_SCHEMA),
        "read":      ("read",      SPI_READ_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False
        self._mosi_pin: str | None = None
        self._miso_pin: str | None = None
        self._cs_pin: str | None = None

    def configure(
        self,
        clk_pin: str,
        frequency_hz: float,
        mode: int,
        mosi_pin: str | None = None,
        miso_pin: str | None = None,
        cs_pin: str | None = None,
        cs_polarity: str = "active_low",
        bit_order: str = "msb",
    ) -> dict[str, Any]:
        pins = [p for p in [clk_pin, mosi_pin, miso_pin, cs_pin] if p is not None]
        for p in pins:
            self.device.validate_pin(p)
        clk_idx = _dio_index(clk_pin)
        mosi_idx = _dio_index(mosi_pin) if mosi_pin else None
        miso_idx = _dio_index(miso_pin) if miso_pin else None
        cs_idx = _dio_index(cs_pin) if cs_pin else None

        self.device.allocator.claim("spi", ["spi_engine"] + pins)
        self._configured = False
        self._mosi_pin = None
        self._miso_pin = None
        self._cs_pin = None
        try:
            self.device.backend.spi_configure(
                clk_idx=clk_idx, freq_hz=frequency_hz, mode=mode,
                mosi_idx=mosi_idx, miso_idx=miso_idx, cs_idx=cs_idx,
                cs_polarity=cs_polarity, bit_order=bit_order,
            )
        except Exception:
            self.device.allocator.release("spi")
            raise
        self._configured = True
        self._mosi_pin = mosi_pin
        self._miso_pin = miso_pin
        self._cs_pin = cs_pin
        return {
            "configured": True,
            "clk_pin": clk_pin,
            "frequency_hz": frequency_hz,
            "mode": mode,
            "cs_polarity": cs_polarity,
            "bit_order": bit_order,
        }

    def transfer(self, data: list[int], assert_cs: bool = True) -> dict[str, Any]:
        self._require_configured()
        if self._mosi_pin is None:
            raise InstrumentNotConfigured("spi.transfer requires mosi_pin to be configured")
        if self._miso_pin is None:
            raise InstrumentNotConfigured("spi.transfer requires miso_pin to be configured")
        if assert_cs and self._cs_pin is None:
            raise InstrumentNotConfigured(
                "spi.transfer with assert_cs=True requires cs_pin to be configured"
            )
        received = self.device.backend.spi_transfer(bytes(data), assert_cs)
        return {"sent": list(data), "received": list(received)}

    def write(self, data: list[int], assert_cs: bool = True) -> dict[str, Any]:
        self._require_configured()
        if self._mosi_pin is None:
            raise InstrumentNotConfigured("spi.write requires mosi_pin to be configured")
        if assert_cs and self._cs_pin is None:
            raise InstrumentNotConfigured(
                "spi.write with assert_cs=True requires cs_pin to be configured"
            )
        self.device.backend.spi_write(bytes(data), assert_cs)
        return {"bytes_written": len(data)}

    def read(self, length: int, assert_cs: bool = True) -> dict[str, Any]:
        self._require_configured()
        if self._miso_pin is None:
            raise InstrumentNotConfigured("spi.read requires miso_pin to be configured")
        if assert_cs and self._cs_pin is None:
            raise InstrumentNotConfigured(
                "spi.read with assert_cs=True requires cs_pin to be configured"
            )
        data = self.device.backend.spi_read(length, assert_cs)
        return {"data": list(data), "data_hex": data.hex()}

    def release(self) -> None:
        self.device.allocator.release("spi")
        self._configured = False
        self._mosi_pin = None
        self._miso_pin = None
        self._cs_pin = None

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("spi.configure must be called before any I/O operation")
