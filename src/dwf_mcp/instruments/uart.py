from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.pin_utils import dio_index as _dio_index

UART_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["baud_rate"],
    "properties": {
        "baud_rate": {"type": "integer", "minimum": 300, "maximum": 4_000_000},
        "tx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "rx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {
            "type": "string", "enum": ["none", "odd", "even"], "default": "none",
        },
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
    },
}

UART_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["data"],
    "properties": {
        "data": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
        },
    },
}

UART_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["length"],
    "properties": {
        "length": {"type": "integer", "minimum": 1, "maximum": 65536},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}


class UART(Instrument):
    name = "uart"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", UART_CONFIGURE_SCHEMA),
        "write":     ("write",     UART_WRITE_SCHEMA),
        "read":      ("read",      UART_READ_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False
        self._tx_pin: str | None = None
        self._rx_pin: str | None = None

    def configure(
        self,
        baud_rate: int,
        tx_pin: str | None = None,
        rx_pin: str | None = None,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
    ) -> dict[str, Any]:
        if tx_pin is None and rx_pin is None:
            raise ValueError("at least one of tx_pin or rx_pin must be provided")
        pins = [p for p in [tx_pin, rx_pin] if p is not None]
        for p in pins:
            self.device.validate_pin(p)
        tx_idx = _dio_index(tx_pin) if tx_pin else None
        rx_idx = _dio_index(rx_pin) if rx_pin else None

        self.device.allocator.claim("uart", ["uart_engine"] + pins)
        self._configured = False
        self._tx_pin = None
        self._rx_pin = None
        try:
            self.device.backend.uart_configure(
                baud_rate=baud_rate, tx_idx=tx_idx, rx_idx=rx_idx,
                data_bits=data_bits, parity=parity, stop_bits=stop_bits,
            )
        except Exception:
            self.device.allocator.release("uart")
            raise
        self._configured = True
        self._tx_pin = tx_pin
        self._rx_pin = rx_pin
        return {
            "configured": True,
            "baud_rate": baud_rate,
            "tx_pin": tx_pin,
            "rx_pin": rx_pin,
            "parity": parity,
            "data_bits": data_bits,
            "stop_bits": stop_bits,
        }

    def write(self, data: list[int]) -> dict[str, Any]:
        self._require_configured()
        if self._tx_pin is None:
            raise InstrumentNotConfigured("uart.write requires tx_pin to be configured")
        self.device.backend.uart_write(bytes(data))
        return {"bytes_written": len(data)}

    def read(self, length: int, timeout_s: float = 1.0) -> dict[str, Any]:
        self._require_configured()
        if self._rx_pin is None:
            raise InstrumentNotConfigured("uart.read requires rx_pin to be configured")
        data, parity_error = self.device.backend.uart_read(length, timeout_s)
        return {"data": list(data), "data_hex": data.hex(), "parity_error": parity_error}

    def release(self) -> None:
        self.device.allocator.release("uart")
        self._configured = False
        self._tx_pin = None
        self._rx_pin = None

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("uart.configure must be called before any I/O operation")
