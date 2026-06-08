"""Sniff instrument: passive protocol capture using hardware protocol engines."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

log = logging.getLogger(__name__)

_PIN_RE = r"^dio([0-9]|1[0-5])$"


def _dio_index(pin: str) -> int:
    return int(pin[3:])


def _pins_to_mask(pins: list[str]) -> int:
    mask = 0
    for p in pins:
        mask |= 1 << int(p[3:])
    return mask


SNIFF_I2C_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sda_pin", "scl_pin", "duration_s"],
    "properties": {
        "sda_pin": {"type": "string", "pattern": _PIN_RE},
        "scl_pin": {"type": "string", "pattern": _PIN_RE},
        "duration_s": {"type": "number", "minimum": 0.001},
        "clock_hz": {"type": "number", "default": 400000},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SNIFF_UART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rx_pin", "baud", "duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "baud": {"type": "integer", "minimum": 300},
        "duration_s": {"type": "number", "minimum": 0.001},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {"type": "string", "enum": ["none", "odd", "even"], "default": "none"},
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SNIFF_CAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rx_pin", "bitrate", "duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "bitrate": {"type": "integer", "minimum": 10_000},
        "duration_s": {"type": "number", "minimum": 0.001},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SPI_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["clk_pin", "mosi_pin", "mode", "freq_hz"],
    "properties": {
        "clk_pin": {"type": "string", "pattern": _PIN_RE},
        "mosi_pin": {"type": "string", "pattern": _PIN_RE},
        "miso_pin": {"type": "string", "pattern": _PIN_RE},
        "cs_pin": {"type": "string", "pattern": _PIN_RE},
        "mode": {"type": "integer", "enum": [0, 1, 2, 3]},
        "freq_hz": {"type": "number", "minimum": 1.0},
        "poll_interval_s": {"type": "number", "default": 0.010},
        "output_path": {"type": "string"},
    },
}

SPI_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sniff_id"],
    "properties": {"sniff_id": {"type": "string"}},
}

SPI_STOP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sniff_id"],
    "properties": {"sniff_id": {"type": "string"}},
}


class Sniff(Instrument):
    name = "sniff"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "i2c":        ("i2c",        SNIFF_I2C_SCHEMA),
        "uart":       ("uart",       SNIFF_UART_SCHEMA),
        "can":        ("can",        SNIFF_CAN_SCHEMA),
        "spi_start":  ("spi_start",  SPI_START_SCHEMA),
        "spi_status": ("spi_status", SPI_STATUS_SCHEMA),
        "spi_stop":   ("spi_stop",   SPI_STOP_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._spi_sessions: dict[str, Any] = {}

    # --- sniff.i2c ---

    async def i2c(
        self,
        sda_pin: str,
        scl_pin: str,
        duration_s: float,
        clock_hz: float = 400_000,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        import time
        self.device.allocator.claim("sniff_i2c", ["i2c_engine", sda_pin, scl_pin])
        transactions: list[dict[str, Any]] = []
        error_count = 0
        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            self.device.backend.i2c_spy_start()
            deadline = time.monotonic() + duration_s
            pending_bytes: list[int] = []
            in_transaction = False

            while time.monotonic() < deadline:
                await asyncio.sleep(poll_interval_s)
                start, stop, data, nak = self.device.backend.i2c_spy_status(256)

                if start:
                    if in_transaction and pending_bytes:
                        _close_i2c_transaction(pending_bytes, nak, transactions)
                        if nak:
                            error_count += 1
                    pending_bytes = list(data)
                    in_transaction = True
                elif data:
                    pending_bytes.extend(data)

                if stop and in_transaction and pending_bytes:
                    _close_i2c_transaction(pending_bytes, nak, transactions)
                    if nak:
                        error_count += 1
                    pending_bytes = []
                    in_transaction = False

            try:
                result = self.artifacts.write_parquet(
                    "sniff_i2c",
                    transactions,
                    config={
                        "sda_pin": sda_pin, "scl_pin": scl_pin,
                        "clock_hz": clock_hz, "duration_s": duration_s,
                        "poll_interval_s": poll_interval_s,
                    },
                    output_path=output_path,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.i2c artifact write failed")
                artifact_error = str(exc)
        finally:
            with suppress(Exception):
                self.device.backend.i2c_spy_stop()
            self.device.allocator.release("sniff_i2c")

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": len(transactions),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {"first_n": [_summarise_i2c(t) for t in transactions[:5]]},
        }

    # --- sniff.uart ---

    async def uart(
        self,
        rx_pin: str,
        baud: int,
        duration_s: float,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        self.device.allocator.claim("sniff_uart", ["uart_engine", rx_pin])
        artifact_path: str | None = None
        artifact_error: str | None = None
        raw_frames: list[tuple[float, bytes, bool]] = []
        error_count = 0
        try:
            raw_frames = self.device.backend.uart_sniff(
                rx_pin_idx=_dio_index(rx_pin),
                baud=baud,
                data_bits=data_bits,
                parity=parity,
                stop_bits=stop_bits,
                duration_s=duration_s,
                poll_interval_s=poll_interval_s,
            )
            error_count = sum(1 for _, _, pe in raw_frames if pe)
            records = [
                {
                    "timestamp_s": ts,
                    "data": data,
                    "parity_error": pe,
                    "framing_error": None,
                    "break_condition": None,
                    "error": pe,
                    "error_detail": "parity error" if pe else None,
                }
                for ts, data, pe in raw_frames
            ]
            try:
                result = self.artifacts.write_parquet(
                    "sniff_uart", records,
                    config={
                        "rx_pin": rx_pin, "baud": baud, "data_bits": data_bits,
                        "parity": parity, "stop_bits": stop_bits,
                        "duration_s": duration_s, "poll_interval_s": poll_interval_s,
                    },
                    output_path=output_path,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.uart artifact write failed")
                artifact_error = str(exc)
        finally:
            self.device.allocator.release("sniff_uart")

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": len(raw_frames),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {},
        }

    # --- sniff.can ---

    async def can(
        self,
        rx_pin: str,
        bitrate: int,
        duration_s: float,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        self.device.allocator.claim("sniff_can", ["can_engine", rx_pin])
        artifact_path: str | None = None
        artifact_error: str | None = None
        raw_frames: list[tuple[float, int, bytes, bool, int]] = []
        error_count = 0
        records: list[dict[str, Any]] = []
        try:
            raw_frames = self.device.backend.can_sniff(
                rx_pin_idx=_dio_index(rx_pin),
                bitrate=bitrate,
                duration_s=duration_s,
                poll_interval_s=poll_interval_s,
            )
            error_count = sum(1 for *_, ec in raw_frames if ec)
            records = [
                {
                    "timestamp_s": ts,
                    "frame_id": fid,
                    "extended": ext,
                    "rtr": False,
                    "dlc": len(data),
                    "data": data,
                    "crc_valid": None,
                    "ack_received": None,
                    "error_type": None,
                    "error": bool(ec),
                    "error_detail": f"error_count={ec}" if ec else None,
                }
                for ts, fid, data, ext, ec in raw_frames
            ]
            try:
                result = self.artifacts.write_parquet(
                    "sniff_can", records,
                    config={"rx_pin": rx_pin, "bitrate": bitrate, "duration_s": duration_s},
                    output_path=output_path,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.can artifact write failed")
                artifact_error = str(exc)
        finally:
            self.device.allocator.release("sniff_can")

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": len(records),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {},
        }

    # --- spi_start / spi_status / spi_stop (implemented in Task 9) ---

    async def spi_start(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("implement in Task 9")

    def spi_status(self, sniff_id: str) -> dict[str, Any]:
        raise NotImplementedError("implement in Task 9")

    async def spi_stop(self, sniff_id: str) -> dict[str, Any]:
        raise NotImplementedError("implement in Task 9")

    def release(self) -> None:
        self.device.allocator.release("sniff_i2c")
        self.device.allocator.release("sniff_uart")
        self.device.allocator.release("sniff_can")
        for sniff_id in list(self._spi_sessions):
            with suppress(Exception):
                self.device.backend.logic_record_stop()
            self.device.allocator.release(f"sniff_spi_{sniff_id}")
        self._spi_sessions.clear()


def _close_i2c_transaction(
    pending_bytes: list[int], nak: int, out: list[dict[str, Any]]
) -> None:
    if not pending_bytes:
        return
    addr_byte = pending_bytes[0]
    address = addr_byte >> 1
    direction = "read" if (addr_byte & 1) else "write"
    data = bytes(pending_bytes[1:])
    # NAK encoding: verify empirically at implementation time (TODO per spec).
    nak_at_byte: int | None = nak if nak else None
    out.append({
        "timestamp_s": 0.0,
        "type": direction,
        "address": address,
        "address_bits": 7,
        "data": data,
        "nak_at_byte": nak_at_byte,
        "error": bool(nak),
        "error_detail": f"nak at byte {nak_at_byte}" if nak_at_byte is not None else None,
    })


def _summarise_i2c(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": t["type"],
        "address": hex(t["address"]),
        "data_len": len(t["data"]) if t["data"] else 0,
    }
