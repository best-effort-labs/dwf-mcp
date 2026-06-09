"""Sniff instrument: passive protocol capture using hardware protocol engines."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument
from dwf_mcp.instruments._async_sniff import (
    _AsyncSniffSession,
    check_memory_cap,
    reap_completed_sessions,
    start_observe_session,
    stop_observe_session,
    stream_observe_session,
)
from dwf_mcp.instruments.decoder.can import CanDecoder
from dwf_mcp.instruments.decoder.i2c import I2cDecoder
from dwf_mcp.instruments.decoder.uart import UartDecoder

log = logging.getLogger(__name__)

_PIN_RE = r"^dio([0-9]|1[0-5])$"


def _dio_index(pin: str) -> int:
    return int(pin[3:])


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
        "polarity": {"type": "integer", "enum": [0, 1], "default": 0,
                     "description": "pydwf protocol.uart.polaritySet value; 0 = standard TTL (idle HIGH); 1 = inverted"},
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
    "required": ["clk_pin", "mosi_pin", "mode", "freq_hz", "max_duration_s"],
    "properties": {
        "clk_pin": {"type": "string", "pattern": _PIN_RE},
        "mosi_pin": {"type": "string", "pattern": _PIN_RE},
        "miso_pin": {"type": "string", "pattern": _PIN_RE},
        "cs_pin": {"type": "string", "pattern": _PIN_RE},
        "mode": {"type": "integer", "enum": [0, 1, 2, 3]},
        "freq_hz": {"type": "number", "minimum": 1.0},
        "max_duration_s": {"type": "number", "minimum": 0.001, "maximum": 3600.0},
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

SNIFF_I2C_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sda_pin", "scl_pin", "clock_hz", "max_duration_s"],
    "properties": {
        "sda_pin": {"type": "string", "pattern": _PIN_RE},
        "scl_pin": {"type": "string", "pattern": _PIN_RE},
        "clock_hz": {"type": "integer", "minimum": 1_000},
        "max_duration_s": {"type": "number", "minimum": 0.001, "maximum": 3600.0},
        "sample_rate_hz": {"type": "number", "minimum": 1_000.0},
        "output_path": {"type": "string"},
    },
}

SNIFF_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sniff_id"],
    "properties": {"sniff_id": {"type": "string"}},
}

SNIFF_STOP_SCHEMA: dict[str, Any] = SNIFF_STATUS_SCHEMA

SNIFF_UART_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rx_pin", "baud", "max_duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "baud": {"type": "integer", "minimum": 300},
        "max_duration_s": {"type": "number", "minimum": 0.001, "maximum": 3600.0},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {"type": "string", "enum": ["none", "odd", "even"], "default": "none"},
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
        "polarity": {"type": "integer", "enum": [0, 1], "default": 0},
        "sample_rate_hz": {"type": "number", "minimum": 1_000.0},
        "output_path": {"type": "string"},
    },
}

SNIFF_CAN_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rx_pin", "bitrate", "max_duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "bitrate": {"type": "integer", "minimum": 10_000},
        "max_duration_s": {"type": "number", "minimum": 0.001, "maximum": 3600.0},
        "sample_rate_hz": {"type": "number", "minimum": 1_000.0},
        "output_path": {"type": "string"},
    },
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
        "i2c_start":  ("i2c_start",  SNIFF_I2C_START_SCHEMA),
        "i2c_status": ("i2c_status", SNIFF_STATUS_SCHEMA),
        "i2c_stop":   ("i2c_stop",   SNIFF_STOP_SCHEMA),
        "uart_start":  ("uart_start",  SNIFF_UART_START_SCHEMA),
        "uart_status": ("uart_status", SNIFF_STATUS_SCHEMA),
        "uart_stop":   ("uart_stop",   SNIFF_STOP_SCHEMA),
        "can_start":   ("can_start",   SNIFF_CAN_START_SCHEMA),
        "can_status":  ("can_status",  SNIFF_STATUS_SCHEMA),
        "can_stop":    ("can_stop",    SNIFF_STOP_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._spi_sessions: dict[str, _AsyncSniffSession] = {}
        # Shared session dict for the async observe-mode sniff tools
        # (i2c_*, uart_*, can_*). SPI keeps its own dict for backwards
        # compatibility with existing tests that reach into ``_spi_sessions``.
        self._async_sessions: dict[str, _AsyncSniffSession] = {}

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
        self.device.allocator.claim("sniff_i2c", ["i2c_engine", sda_pin, scl_pin])
        transactions: list[dict[str, Any]] = []
        error_count = 0
        artifact_path: str | None = None
        artifact_error: str | None = None
        spy_started = False
        try:
            self.device.backend.i2c_configure(
                scl_pin_idx=_dio_index(scl_pin),
                sda_pin_idx=_dio_index(sda_pin),
                rate_hz=clock_hz,
                stretch=False,
                timeout_s=0.0,
            )
            self.device.backend.i2c_spy_start()
            spy_started = True
            start_time = time.monotonic()
            deadline = start_time + duration_s
            pending_bytes: list[int] = []
            in_transaction = False

            while time.monotonic() < deadline:
                await asyncio.sleep(poll_interval_s)
                current_ts = time.monotonic() - start_time
                start, stop, data, nak = self.device.backend.i2c_spy_status(256)

                if start:
                    if in_transaction and pending_bytes:
                        _close_i2c_transaction(pending_bytes, nak, transactions, current_ts)
                        if nak:
                            error_count += 1
                    pending_bytes = list(data)
                    in_transaction = True
                elif data:
                    pending_bytes.extend(data)

                if stop and in_transaction and pending_bytes:
                    _close_i2c_transaction(pending_bytes, nak, transactions, current_ts)
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
                    output_path=Path(output_path) if output_path else None,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.i2c artifact write failed")
                artifact_error = str(exc)
        finally:
            if spy_started:
                try:
                    self.device.backend.i2c_spy_stop()
                except Exception as exc:
                    log.warning("i2c_spy_stop failed: %s", exc)
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
        polarity: int = 0,
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
                polarity=polarity,
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
                        "parity": parity, "stop_bits": stop_bits, "polarity": polarity,
                        "duration_s": duration_s, "poll_interval_s": poll_interval_s,
                    },
                    output_path=Path(output_path) if output_path else None,
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
                    output_path=Path(output_path) if output_path else None,
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

    # --- spi_start / spi_status / spi_stop ---

    async def spi_start(
        self,
        clk_pin: str,
        mosi_pin: str,
        mode: int,
        freq_hz: float,
        max_duration_s: float,
        miso_pin: str | None = None,
        cs_pin: str | None = None,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        sample_rate_hz = freq_hz * 10  # 10× oversampling
        pins = [p for p in [clk_pin, mosi_pin, miso_pin, cs_pin] if p is not None]
        check_memory_cap(sample_rate_hz, max_duration_s, n_pins=len(pins))
        pin_mask = sum(1 << int(p[3:]) for p in pins)

        sniff_id = str(uuid.uuid4())
        allocator_key = f"sniff_spi_{sniff_id}"
        meta: dict[str, Any] = {
            "sniff_id": sniff_id,
            "pins": pins,
            "sample_rate_hz": sample_rate_hz,
            "max_duration_s": max_duration_s,
            "clk_pin": clk_pin,
            "mosi_pin": mosi_pin,
            "miso_pin": miso_pin,
            "cs_pin": cs_pin,
            "mode": mode,
            "output_path": output_path,
        }
        session = start_observe_session(
            device=self.device,
            allocator_key=allocator_key,
            pin_mask=pin_mask,
            sample_rate_hz=sample_rate_hz,
            max_duration_s=max_duration_s,
            meta=meta,
        )
        self._spi_sessions[sniff_id] = session
        reap_completed_sessions(self._spi_sessions, self.device)
        return {"sniff_id": sniff_id}

    def spi_status(self, sniff_id: str) -> dict[str, Any]:
        reap_completed_sessions(self._spi_sessions, self.device)
        session = self._spi_sessions.get(sniff_id)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")
        r = session.record_session
        total_samples = sum(len(c) for c in r.chunks)
        return {
            "samples_received": total_samples,
            "lost_samples": r.lost_samples,
            "done": r.done,
        }

    async def spi_stop(self, sniff_id: str) -> dict[str, Any]:
        from dwf_mcp.instruments.decoder.spi import SpiDecoder

        session = self._spi_sessions.pop(sniff_id, None)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")

        artifact_path: str | None = None
        artifact_error: str | None = None
        count = 0
        error_count = 0
        try:
            meta = session.meta
            pins = meta["pins"]
            pin_map: dict[str, int] = {
                "clk":  int(meta["clk_pin"][3:]),
                "mosi": int(meta["mosi_pin"][3:]),
            }
            if meta["miso_pin"] and meta["miso_pin"] in pins:
                pin_map["miso"] = int(meta["miso_pin"][3:])
            if meta["cs_pin"] and meta["cs_pin"] in pins:
                pin_map["cs"] = int(meta["cs_pin"][3:])

            decoder = SpiDecoder()
            decoder.init(
                pin_map, sample_rate_hz=meta["sample_rate_hz"], mode=meta["mode"],
            )
            txns, lost_samples = await stream_observe_session(
                session, self.device, decoder,
            )
            try:
                count = len(txns)
                error_count = sum(1 for t in txns if t.error)
                records = [t.to_dict() for t in txns]
                result = self.artifacts.write_parquet(
                    "sniff_spi", records,
                    config={k: v for k, v in meta.items() if k != "sniff_id"},
                    output_path=meta.get("output_path"),
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("spi_stop decode/write failed for sniff_id=%r", sniff_id)
                artifact_error = str(exc)
        finally:
            self.device.allocator.release(session.allocator_key)

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": count,
            "error_count": error_count,
            "lost_samples": lost_samples,
            "artifact_error": artifact_error,
            "summary": {},
        }

    # --- sniff.i2c_start / i2c_status / i2c_stop (async observe-mode) ---

    async def i2c_start(
        self,
        sda_pin: str,
        scl_pin: str,
        clock_hz: int,
        max_duration_s: float,
        sample_rate_hz: float | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        rate = float(sample_rate_hz) if sample_rate_hz else float(clock_hz) * 10.0
        if rate / float(clock_hz) < 4.0:
            raise ValueError(
                f"I2C decode requires >=4x oversampling, got {rate / float(clock_hz):.1f}x"
            )
        check_memory_cap(rate, max_duration_s, n_pins=2)

        sniff_id = str(uuid.uuid4())
        allocator_key = f"sniff_i2c_{sniff_id}"
        pin_mask = (1 << _dio_index(sda_pin)) | (1 << _dio_index(scl_pin))
        meta: dict[str, Any] = {
            "sniff_id": sniff_id,
            "sda_pin": sda_pin,
            "scl_pin": scl_pin,
            "clock_hz": clock_hz,
            "sample_rate_hz": rate,
            "max_duration_s": max_duration_s,
            "output_path": output_path,
        }
        session = start_observe_session(
            device=self.device,
            allocator_key=allocator_key,
            pin_mask=pin_mask,
            sample_rate_hz=rate,
            max_duration_s=max_duration_s,
            meta=meta,
        )
        self._async_sessions[sniff_id] = session
        reap_completed_sessions(self._async_sessions, self.device)
        return {"sniff_id": sniff_id}

    def i2c_status(self, sniff_id: str) -> dict[str, Any]:
        reap_completed_sessions(self._async_sessions, self.device)
        session = self._async_sessions.get(sniff_id)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")
        rs = session.record_session
        total = sum(len(c) for c in rs.chunks)
        return {
            "samples_received": total,
            "lost_samples": rs.lost_samples,
            "done": rs.done,
        }

    async def i2c_stop(self, sniff_id: str) -> dict[str, Any]:
        session = self._async_sessions.pop(sniff_id, None)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")

        artifact_path: str | None = None
        artifact_error: str | None = None
        count = 0
        error_count = 0
        txns: list[Any] = []
        try:
            meta = session.meta
            decoder = I2cDecoder()
            decoder.init(
                {
                    "sda": _dio_index(meta["sda_pin"]),
                    "scl": _dio_index(meta["scl_pin"]),
                },
                sample_rate_hz=meta["sample_rate_hz"],
            )
            txns, lost_samples = await stream_observe_session(
                session, self.device, decoder,
            )
            try:
                records = [t.to_dict() for t in txns]
                # Assign count/error_count BEFORE write so a parquet failure
                # (disk full, etc.) doesn't zero out a successful decode.
                count = len(txns)
                error_count = sum(1 for t in txns if t.error)
                result = self.artifacts.write_parquet(
                    "sniff_i2c",
                    records,
                    config={k: v for k, v in meta.items() if k != "sniff_id"},
                    output_path=Path(meta["output_path"]) if meta.get("output_path") else None,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.i2c_stop decode/write failed for %s", sniff_id)
                artifact_error = str(exc)
        finally:
            self.device.allocator.release(session.allocator_key)

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        # Match engine-mode sniff.i2c's summary shape: first_n summaries of
        # decoded transactions. _summarise_i2c expects a dict, so feed it
        # the dataclass's to_dict().
        first_n = [_summarise_i2c(t.to_dict()) for t in txns[:5]]
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": count,
            "error_count": error_count,
            "lost_samples": lost_samples,
            "artifact_error": artifact_error,
            "summary": {"first_n": first_n},
        }

    # --- sniff.uart_start / uart_status / uart_stop (async observe-mode) ---

    async def uart_start(
        self,
        rx_pin: str,
        baud: int,
        max_duration_s: float,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
        polarity: int = 0,
        sample_rate_hz: float | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        rate = float(sample_rate_hz) if sample_rate_hz else float(baud) * 10.0
        if rate / float(baud) < 4.0:
            raise ValueError(
                f"UART decode requires >=4x oversampling, got {rate / float(baud):.1f}x"
            )
        check_memory_cap(rate, max_duration_s, n_pins=1)

        sniff_id = str(uuid.uuid4())
        allocator_key = f"sniff_uart_{sniff_id}"
        pin_mask = 1 << _dio_index(rx_pin)
        meta: dict[str, Any] = {
            "sniff_id": sniff_id,
            "rx_pin": rx_pin,
            "baud": baud,
            "data_bits": data_bits,
            "parity": parity,
            "stop_bits": stop_bits,
            "polarity": polarity,
            "sample_rate_hz": rate,
            "max_duration_s": max_duration_s,
            "output_path": output_path,
        }
        session = start_observe_session(
            device=self.device,
            allocator_key=allocator_key,
            pin_mask=pin_mask,
            sample_rate_hz=rate,
            max_duration_s=max_duration_s,
            meta=meta,
        )
        self._async_sessions[sniff_id] = session
        reap_completed_sessions(self._async_sessions, self.device)
        return {"sniff_id": sniff_id}

    def uart_status(self, sniff_id: str) -> dict[str, Any]:
        reap_completed_sessions(self._async_sessions, self.device)
        session = self._async_sessions.get(sniff_id)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")
        rs = session.record_session
        total = sum(len(c) for c in rs.chunks)
        return {
            "samples_received": total,
            "lost_samples": rs.lost_samples,
            "done": rs.done,
        }

    async def uart_stop(self, sniff_id: str) -> dict[str, Any]:
        session = self._async_sessions.pop(sniff_id, None)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")

        artifact_path: str | None = None
        artifact_error: str | None = None
        count = 0
        error_count = 0
        try:
            meta = session.meta
            decoder = UartDecoder()
            decoder.init(
                {"rx": _dio_index(meta["rx_pin"])},
                sample_rate_hz=meta["sample_rate_hz"],
                baud=meta["baud"],
                data_bits=meta["data_bits"],
                parity=meta["parity"],
                stop_bits=meta["stop_bits"],
                polarity=meta["polarity"],
            )
            frames, lost_samples = await stream_observe_session(
                session, self.device, decoder,
            )
            try:
                records = [f.to_dict() for f in frames]
                # Assign count/error_count BEFORE write so a parquet failure
                # (disk full, etc.) doesn't zero out a successful decode.
                count = len(frames)
                error_count = sum(1 for f in frames if f.error)
                result = self.artifacts.write_parquet(
                    "sniff_uart",
                    records,
                    config={k: v for k, v in meta.items() if k != "sniff_id"},
                    output_path=Path(meta["output_path"]) if meta.get("output_path") else None,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.uart_stop decode/write failed for %s", sniff_id)
                artifact_error = str(exc)
        finally:
            self.device.allocator.release(session.allocator_key)

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": count,
            "error_count": error_count,
            "lost_samples": lost_samples,
            "artifact_error": artifact_error,
            "summary": {},
        }

    # --- sniff.can_start / can_status / can_stop (async observe-mode) ---

    async def can_start(
        self,
        rx_pin: str,
        bitrate: int,
        max_duration_s: float,
        sample_rate_hz: float | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        # Default 20× oversampling — CAN spec requires >=8× per bit; 20× gives
        # safe headroom for the 75 % sample point and bit-stuff destuffing.
        rate = float(sample_rate_hz) if sample_rate_hz else float(bitrate) * 20.0
        if rate / float(bitrate) < 8.0:
            raise ValueError(
                f"CAN decode requires >=8x oversampling, got {rate / float(bitrate):.1f}x"
            )
        check_memory_cap(rate, max_duration_s, n_pins=1)

        sniff_id = str(uuid.uuid4())
        allocator_key = f"sniff_can_{sniff_id}"
        pin_mask = 1 << _dio_index(rx_pin)
        meta: dict[str, Any] = {
            "sniff_id": sniff_id,
            "rx_pin": rx_pin,
            "bitrate": bitrate,
            "sample_rate_hz": rate,
            "max_duration_s": max_duration_s,
            "output_path": output_path,
        }
        session = start_observe_session(
            device=self.device,
            allocator_key=allocator_key,
            pin_mask=pin_mask,
            sample_rate_hz=rate,
            max_duration_s=max_duration_s,
            meta=meta,
        )
        self._async_sessions[sniff_id] = session
        reap_completed_sessions(self._async_sessions, self.device)
        return {"sniff_id": sniff_id}

    def can_status(self, sniff_id: str) -> dict[str, Any]:
        reap_completed_sessions(self._async_sessions, self.device)
        session = self._async_sessions.get(sniff_id)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")
        rs = session.record_session
        total = sum(len(c) for c in rs.chunks)
        return {
            "samples_received": total,
            "lost_samples": rs.lost_samples,
            "done": rs.done,
        }

    async def can_stop(self, sniff_id: str) -> dict[str, Any]:
        session = self._async_sessions.pop(sniff_id, None)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")

        artifact_path: str | None = None
        artifact_error: str | None = None
        count = 0
        error_count = 0
        try:
            meta = session.meta
            decoder = CanDecoder()
            decoder.init(
                {"rx": _dio_index(meta["rx_pin"])},
                sample_rate_hz=meta["sample_rate_hz"],
                bitrate=meta["bitrate"],
            )
            frames, lost_samples = await stream_observe_session(
                session, self.device, decoder,
            )
            try:
                records = [f.to_dict() for f in frames]
                # Assign count/error_count BEFORE write so a parquet failure
                # (disk full, etc.) doesn't zero out a successful decode.
                count = len(frames)
                error_count = sum(1 for f in frames if f.error)
                result = self.artifacts.write_parquet(
                    "sniff_can",
                    records,
                    config={k: v for k, v in meta.items() if k != "sniff_id"},
                    output_path=Path(meta["output_path"]) if meta.get("output_path") else None,
                )
                artifact_path = result.path
            except Exception as exc:
                log.exception("sniff.can_stop decode/write failed for %s", sniff_id)
                artifact_error = str(exc)
        finally:
            self.device.allocator.release(session.allocator_key)

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": count,
            "error_count": error_count,
            "lost_samples": lost_samples,
            "artifact_error": artifact_error,
            "summary": {},
        }

    def release(self) -> None:
        self.device.allocator.release("sniff_i2c")
        self.device.allocator.release("sniff_uart")
        self.device.allocator.release("sniff_can")
        for sniff_id, session in list(self._spi_sessions.items()):
            # Cancel background record_loop / notification_loop tasks so they don't
            # keep polling the backend after release. cancel() is sync-safe; the
            # CancelledError is delivered to the task on its next await point.
            r = session.record_session
            if r.task is not None and not r.task.done():
                r.task.cancel()
            if r.notification_task is not None and not r.notification_task.done():
                r.notification_task.cancel()
            try:
                self.device.backend.logic_record_stop()
            except Exception as exc:
                log.warning("logic_record_stop during sniff.release for %s failed: %s",
                            sniff_id, exc)
            self.device.allocator.release(session.allocator_key)
        self._spi_sessions.clear()

        for sniff_id, session in list(self._async_sessions.items()):
            r = session.record_session
            if r.task is not None and not r.task.done():
                r.task.cancel()
            if r.notification_task is not None and not r.notification_task.done():
                r.notification_task.cancel()
            try:
                self.device.backend.logic_record_stop()
            except Exception as exc:
                log.warning("logic_record_stop during sniff.release for %s failed: %s",
                            sniff_id, exc)
            self.device.allocator.release(session.allocator_key)
        self._async_sessions.clear()


def _close_i2c_transaction(
    pending_bytes: list[int], nak: int, out: list[dict[str, Any]], timestamp_s: float = 0.0
) -> None:
    if not pending_bytes:
        return
    addr_byte = pending_bytes[0]
    address = addr_byte >> 1
    direction = "read" if (addr_byte & 1) else "write"
    data = bytes(pending_bytes[1:])
    # pydwf i2c.spyStatus returns `nak` as a 1-based byte index where the NAK
    # occurred (counting the address byte as byte 1); 0 means no NAK.
    # We expose `nak_at_byte` as a 0-based index across the full transmission
    # (address = 0, first data byte = 1, ...).
    nak_at_byte: int | None = (nak - 1) if nak > 0 else None
    out.append({
        "timestamp_s": timestamp_s,
        "type": direction,
        "address": address,
        "address_bits": 7,
        "data": data,
        "nak_at_byte": nak_at_byte,
        "error": bool(nak),
        "error_detail": (
            "nak on address byte" if nak_at_byte == 0
            else f"nak on data byte {nak_at_byte - 1}" if nak_at_byte is not None
            else None
        ),
    })


def _summarise_i2c(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": t["type"],
        "address": hex(t["address"]),
        "data_len": len(t["data"]) if t["data"] else 0,
    }
