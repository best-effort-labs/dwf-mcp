"""Decoder instrument: post-process logic capture artifacts."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

log = logging.getLogger(__name__)

_PIN_RE = r"^dio\d+$"

DECODER_SPI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["capture_path", "clk_pin", "mosi_pin"],
    "properties": {
        "capture_path": {"type": "string"},
        "clk_pin":   {"type": "string"},
        "mosi_pin":  {"type": "string"},
        "miso_pin":  {"type": "string"},
        "cs_pin":    {"type": "string"},
        "mode":      {"type": "integer", "enum": [0, 1, 2, 3], "default": 0},
        "bit_order": {"type": "string", "enum": ["msb", "lsb"], "default": "msb"},
        "word_size": {"type": "integer", "minimum": 1, "maximum": 32, "default": 8},
        "output_path": {"type": "string"},
    },
}

DECODER_I2C_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["capture_path", "sda_pin", "scl_pin"],
    "properties": {
        "capture_path": {"type": "string"},
        "sda_pin": {"type": "string", "pattern": _PIN_RE},
        "scl_pin": {"type": "string", "pattern": _PIN_RE},
        "output_path": {"type": "string"},
    },
}

DECODER_UART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["capture_path", "rx_pin", "baud"],
    "properties": {
        "capture_path": {"type": "string"},
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "baud": {"type": "integer", "minimum": 300},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {"type": "string", "enum": ["none", "odd", "even"], "default": "none"},
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
        "polarity": {"type": "integer", "enum": [0, 1], "default": 0},
        "output_path": {"type": "string"},
    },
}

DECODER_CAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["capture_path", "rx_pin", "bitrate"],
    "properties": {
        "capture_path": {"type": "string"},
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "bitrate": {"type": "integer", "minimum": 10_000},
        "output_path": {"type": "string"},
    },
}


class Decoder(Instrument):
    name = "decoder"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "spi":  ("spi",  DECODER_SPI_SCHEMA),
        "i2c":  ("i2c",  DECODER_I2C_SCHEMA),
        "uart": ("uart", DECODER_UART_SCHEMA),
        "can":  ("can",  DECODER_CAN_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts

    async def spi(
        self,
        capture_path: str,
        clk_pin: str,
        mosi_pin: str,
        miso_pin: str | None = None,
        cs_pin: str | None = None,
        mode: int = 0,
        bit_order: str = "msb",
        word_size: int = 8,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        from dwf_mcp.instruments.decoder.spi import SpiDecoder

        for p in [clk_pin, mosi_pin, miso_pin, cs_pin]:
            if p is not None:
                self.device.validate_pin(p)
        loaded = self._load_capture(
            capture_path, {"clk": clk_pin, "mosi": mosi_pin},
        )
        if "error" in loaded:
            return loaded

        # Optional pins are validated and added to pin_map manually.
        captured_pins = loaded["captured_pins"]
        pin_map = loaded["pin_map"]
        for label, pin in [("miso_pin", miso_pin), ("cs_pin", cs_pin)]:
            if pin is None:
                continue
            if pin not in captured_pins:
                return {"error": f"{label}={pin!r} was not captured; available: {captured_pins}"}
            pin_map[label[:-4]] = int(pin[3:])  # "miso_pin" → "miso"

        decoder = SpiDecoder()
        txns = decoder.decode(
            loaded["samples"], pin_map,
            sample_rate_hz=loaded["sample_rate_hz"],
            mode=mode, bit_order=bit_order, word_size=word_size,
        )
        error_count = sum(1 for t in txns if t.error)
        records = [t.to_dict() for t in txns]

        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            result = self.artifacts.write_parquet(
                "decoder_spi", records,
                config={
                    "capture_path": capture_path, "clk_pin": clk_pin,
                    "mosi_pin": mosi_pin, "miso_pin": miso_pin, "cs_pin": cs_pin,
                    "mode": mode, "bit_order": bit_order, "word_size": word_size,
                    "sample_rate_hz": loaded["sample_rate_hz"],
                },
                output_path=Path(output_path) if output_path else None,
            )
            artifact_path = result.path
        except Exception as exc:
            log.exception("decoder.spi artifact write failed")
            artifact_error = str(exc)

        sidecar_out = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_out,
            "count": len(txns),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {"first_n": [t.to_dict() for t in txns[:5]]},
        }

    def _load_capture(
        self, capture_path: str, required_pins: dict[str, str],
    ) -> dict[str, Any]:
        """Load an npz capture + sidecar; return error-or-result dict.

        On success: ``{"samples": np.ndarray, "sample_rate_hz": float,
        "captured_pins": list[str], "pin_map": dict[str, int]}``.
        On failure: ``{"error": "..."}``.

        ``required_pins`` maps signal name (e.g. ``"sda"``) → pin label
        (e.g. ``"dio0"``). Optional pins should be omitted; the caller
        validates and adds them separately.
        """
        npz_path = Path(capture_path)
        sidecar_path = npz_path.with_suffix(".json")
        try:
            sidecar = json.loads(sidecar_path.read_text())
        except Exception as exc:
            return {"error": f"cannot read sidecar {sidecar_path}: {exc}"}

        config = sidecar.get("config", sidecar.get("summary", {}))
        captured_pins: list[str] = config.get("pins", [])
        sample_rate_hz = config.get("sample_rate_hz")
        if sample_rate_hz is None:
            return {"error": "sidecar missing sample_rate_hz; cannot compute timestamps"}

        for label, pin in required_pins.items():
            if pin not in captured_pins:
                return {"error": f"{label}={pin!r} was not captured; available: {captured_pins}"}

        data = np.load(npz_path)
        if not captured_pins:
            return {"error": "sidecar 'pins' list is empty; nothing to decode"}
        n = len(data[captured_pins[0]])
        samples = np.zeros((n, 16), dtype=np.uint8)
        for pin in captured_pins:
            col = int(pin[3:])
            samples[:, col] = data[pin]

        pin_map = {signal: int(pin[3:]) for signal, pin in required_pins.items()}
        return {
            "samples": samples,
            "sample_rate_hz": float(sample_rate_hz),
            "captured_pins": captured_pins,
            "pin_map": pin_map,
        }

    async def i2c(
        self,
        capture_path: str,
        sda_pin: str,
        scl_pin: str,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        from dwf_mcp.instruments.decoder.i2c import I2cDecoder

        self.device.validate_pin(sda_pin)
        self.device.validate_pin(scl_pin)
        loaded = self._load_capture(
            capture_path, {"sda": sda_pin, "scl": scl_pin},
        )
        if "error" in loaded:
            return loaded

        decoder = I2cDecoder()
        txns = decoder.decode(
            loaded["samples"], loaded["pin_map"],
            sample_rate_hz=loaded["sample_rate_hz"],
        )
        error_count = sum(1 for t in txns if t.error)
        records = [t.to_dict() for t in txns]

        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            result = self.artifacts.write_parquet(
                "decoder_i2c", records,
                config={
                    "capture_path": capture_path,
                    "sda_pin": sda_pin, "scl_pin": scl_pin,
                    "sample_rate_hz": loaded["sample_rate_hz"],
                },
                output_path=Path(output_path) if output_path else None,
            )
            artifact_path = result.path
        except Exception as exc:
            log.exception("decoder.i2c artifact write failed")
            artifact_error = str(exc)

        sidecar_out = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_out,
            "count": len(txns),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {"first_n": [t.to_dict() for t in txns[:5]]},
        }

    async def uart(
        self,
        capture_path: str,
        rx_pin: str,
        baud: int,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
        polarity: int = 0,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        from dwf_mcp.instruments.decoder.uart import UartDecoder

        self.device.validate_pin(rx_pin)
        loaded = self._load_capture(capture_path, {"rx": rx_pin})
        if "error" in loaded:
            return loaded

        decoder = UartDecoder()
        frames = decoder.decode(
            loaded["samples"], loaded["pin_map"],
            sample_rate_hz=loaded["sample_rate_hz"],
            baud=baud, data_bits=data_bits, parity=parity,
            stop_bits=stop_bits, polarity=polarity,
        )
        error_count = sum(1 for f in frames if f.error)
        records = [f.to_dict() for f in frames]

        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            result = self.artifacts.write_parquet(
                "decoder_uart", records,
                config={
                    "capture_path": capture_path, "rx_pin": rx_pin,
                    "baud": baud, "data_bits": data_bits, "parity": parity,
                    "stop_bits": stop_bits, "polarity": polarity,
                    "sample_rate_hz": loaded["sample_rate_hz"],
                },
                output_path=Path(output_path) if output_path else None,
            )
            artifact_path = result.path
        except Exception as exc:
            log.exception("decoder.uart artifact write failed")
            artifact_error = str(exc)

        sidecar_out = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_out,
            "count": len(frames),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {"first_n": [f.to_dict() for f in frames[:5]]},
        }

    async def can(
        self,
        capture_path: str,
        rx_pin: str,
        bitrate: int,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        from dwf_mcp.instruments.decoder.can import CanDecoder

        self.device.validate_pin(rx_pin)
        loaded = self._load_capture(capture_path, {"rx": rx_pin})
        if "error" in loaded:
            return loaded

        decoder = CanDecoder()
        frames = decoder.decode(
            loaded["samples"], loaded["pin_map"],
            sample_rate_hz=loaded["sample_rate_hz"],
            bitrate=bitrate,
        )
        error_count = sum(1 for f in frames if f.error)
        records = [f.to_dict() for f in frames]

        artifact_path: str | None = None
        artifact_error: str | None = None
        try:
            result = self.artifacts.write_parquet(
                "decoder_can", records,
                config={
                    "capture_path": capture_path, "rx_pin": rx_pin,
                    "bitrate": bitrate,
                    "sample_rate_hz": loaded["sample_rate_hz"],
                },
                output_path=Path(output_path) if output_path else None,
            )
            artifact_path = result.path
        except Exception as exc:
            log.exception("decoder.can artifact write failed")
            artifact_error = str(exc)

        sidecar_out = artifact_path.replace(".parquet", ".json") if artifact_path else None
        return {
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_out,
            "count": len(frames),
            "error_count": error_count,
            "artifact_error": artifact_error,
            "summary": {"first_n": [f.to_dict() for f in frames[:5]]},
        }

    def release(self) -> None:
        pass
