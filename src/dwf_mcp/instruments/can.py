from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.pin_utils import dio_index as _dio_index

CAN_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tx_pin", "rx_pin", "bit_rate"],
    "properties": {
        "tx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "rx_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "bit_rate": {"type": "integer", "minimum": 1000, "maximum": 1_000_000},
    },
}

CAN_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "data"],
    "properties": {
        "id": {"type": "integer", "minimum": 0, "maximum": 0x1FFFFFFF},
        "data": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
            "maxItems": 8,
        },
        "extended": {"type": "boolean", "default": False},
    },
}

CAN_RECEIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}


class CAN(Instrument):
    name = "can"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", CAN_CONFIGURE_SCHEMA),
        "send":      ("send",      CAN_SEND_SCHEMA),
        "receive":   ("receive",   CAN_RECEIVE_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False

    def configure(
        self,
        tx_pin: str,
        rx_pin: str,
        bit_rate: int,
    ) -> dict[str, Any]:
        self.device.validate_pin(tx_pin)
        self.device.validate_pin(rx_pin)
        tx_idx = _dio_index(tx_pin)
        rx_idx = _dio_index(rx_pin)
        self.device.allocator.claim("can", ["can_engine", tx_pin, rx_pin])
        self._configured = False
        try:
            self.device.backend.can_configure(
                tx_idx=tx_idx, rx_idx=rx_idx, bit_rate=bit_rate,
            )
        except Exception:
            self.device.allocator.release("can")
            raise
        self._configured = True
        return {
            "configured": True,
            "tx_pin": tx_pin,
            "rx_pin": rx_pin,
            "bit_rate": bit_rate,
        }

    def send(self, id: int, data: list[int], extended: bool = False) -> dict[str, Any]:
        self._require_configured()
        if not extended and id > 0x7FF:
            raise ValueError(
                f"standard CAN ID must be ≤ 0x7FF, got {id:#x}; use extended=True for 29-bit IDs"
            )
        self.device.backend.can_send(id=id, data=bytes(data), extended=extended)
        return {"sent": True}

    def receive(self, timeout_s: float = 1.0) -> dict[str, Any]:
        self._require_configured()
        frame_id, data, extended, error_count = self.device.backend.can_receive(timeout_s)
        if frame_id is None:
            return {"id": None, "data": [], "data_hex": "", "extended": False,
                    "error_count": error_count}
        return {
            "id": frame_id,
            "data": list(data),
            "data_hex": data.hex(),
            "extended": extended,
            "error_count": error_count,
        }

    def release(self) -> None:
        self.device.allocator.release("can")
        self._configured = False

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("can.configure must be called before any I/O operation")
