"""Sniff instrument: passive protocol capture using hardware protocol engines."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument
from dwf_mcp.streaming import RecordingSession, record_loop

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
        self.device.allocator.claim("sniff_i2c", ["i2c_engine", sda_pin, scl_pin])
        transactions: list[dict[str, Any]] = []
        error_count = 0
        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            self.device.backend.i2c_spy_start()
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

    # --- spi_start / spi_status / spi_stop ---

    async def spi_start(
        self,
        clk_pin: str,
        mosi_pin: str,
        mode: int,
        freq_hz: float,
        miso_pin: str | None = None,
        cs_pin: str | None = None,
        poll_interval_s: float = 0.010,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        sample_rate_hz = freq_hz * 10  # 10× oversampling
        pins = [p for p in [clk_pin, mosi_pin, miso_pin, cs_pin] if p is not None]
        pin_mask = sum(1 << int(p[3:]) for p in pins)

        sniff_id = str(uuid.uuid4())
        allocator_key = f"sniff_spi_{sniff_id}"
        self.device.allocator.claim_observe(allocator_key)
        try:
            self.device.backend.logic_record_configure(
                pin_mask=pin_mask,
                sample_rate_hz=sample_rate_hz,
                duration_s=3600.0,  # open-ended; spi_stop terminates capture
            )
            self.device.backend.logic_record_arm()
        except Exception:
            with suppress(Exception):
                self.device.backend.logic_record_stop()
            self.device.allocator.release(allocator_key)
            raise

        session = RecordingSession(
            record_id=sniff_id,
            task=None,
            notification_task=None,
            queue=asyncio.Queue(maxsize=32),
            chunks=[],
            lost_samples=0,
            done=False,
            error=None,
            meta={
                "pins": pins,
                "sample_rate_hz": sample_rate_hz,
                "clk_pin": clk_pin,
                "mosi_pin": mosi_pin,
                "miso_pin": miso_pin,
                "cs_pin": cs_pin,
                "mode": mode,
                "output_path": output_path,
                "allocator_key": allocator_key,
            },
        )
        session.task = asyncio.create_task(
            record_loop(
                session,
                self.device.backend.logic_record_status,
                self.device.backend.logic_record_read,
            )
        )
        self._spi_sessions[sniff_id] = session
        return {"sniff_id": sniff_id}

    def spi_status(self, sniff_id: str) -> dict[str, Any]:
        session = self._spi_sessions.get(sniff_id)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")
        total_samples = sum(len(c) for c in session.chunks)
        return {"samples_received": total_samples, "lost_samples": session.lost_samples}

    async def spi_stop(self, sniff_id: str) -> dict[str, Any]:
        import numpy as np
        from dwf_mcp.instruments.decoder.spi import SpiDecoder

        session = self._spi_sessions.pop(sniff_id, None)
        if session is None:
            raise ValueError(f"unknown sniff_id {sniff_id!r}")

        artifact_path: str | None = None
        artifact_error: str | None = None
        count = 0
        error_count = 0
        try:
            # 1. Cancel background task
            if session.task is not None:
                session.task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.task

            # 2. Stop hardware
            with suppress(Exception):
                self.device.backend.logic_record_stop()

            # 3. Drain remaining samples
            try:
                available, lost, _ = self.device.backend.logic_record_status()
                session.lost_samples += lost
                if available > 0:
                    chunk = self.device.backend.logic_record_read(available)
                    session.chunks.append(chunk)
            except Exception as exc:
                log.warning("spi_stop drain failed: %s", exc)

            # 4. Decode
            if session.chunks:
                try:
                    all_samples = np.concatenate(session.chunks, axis=0)
                    meta = session.meta
                    pins = meta["pins"]
                    pin_map: dict[str, int] = {
                        "clk":  pins.index(meta["clk_pin"]),
                        "mosi": pins.index(meta["mosi_pin"]),
                    }
                    if meta["miso_pin"] and meta["miso_pin"] in pins:
                        pin_map["miso"] = pins.index(meta["miso_pin"])
                    if meta["cs_pin"] and meta["cs_pin"] in pins:
                        pin_map["cs"] = pins.index(meta["cs_pin"])

                    decoder = SpiDecoder()
                    txns = decoder.decode(
                        all_samples, pin_map,
                        sample_rate_hz=meta["sample_rate_hz"],
                        mode=meta["mode"],
                    )
                    count = len(txns)
                    error_count = sum(1 for t in txns if t.error)
                    records = [t.to_dict() for t in txns]
                    result = self.artifacts.write_parquet(
                        "sniff_spi", records,
                        config={k: v for k, v in meta.items() if k != "allocator_key"},
                        output_path=meta.get("output_path"),
                    )
                    artifact_path = result.path
                except Exception as exc:
                    log.exception("spi_stop decode/write failed for sniff_id=%r", sniff_id)
                    artifact_error = str(exc)
        finally:
            self.device.allocator.release(session.meta["allocator_key"])

        sidecar_path = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "count": count,
            "error_count": error_count,
            "lost_samples": session.lost_samples,
            "artifact_error": artifact_error,
            "summary": {},
        }

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
    pending_bytes: list[int], nak: int, out: list[dict[str, Any]], timestamp_s: float = 0.0
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
        "timestamp_s": timestamp_s,
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
