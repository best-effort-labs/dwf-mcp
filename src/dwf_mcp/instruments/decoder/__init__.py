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


class Decoder(Instrument):
    name = "decoder"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "spi": ("spi", DECODER_SPI_SCHEMA),
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

        npz_path = Path(capture_path)
        sidecar_path = npz_path.with_suffix(".json")

        # Load sidecar for pin list and sample_rate_hz
        try:
            sidecar = json.loads(sidecar_path.read_text())
        except Exception as exc:
            return {"error": f"cannot read sidecar {sidecar_path}: {exc}"}

        config = sidecar.get("config", sidecar.get("summary", {}))
        captured_pins: list[str] = config.get("pins", [])
        sample_rate_hz = config.get("sample_rate_hz")

        if sample_rate_hz is None:
            return {"error": "sidecar missing sample_rate_hz; cannot compute timestamps"}

        # Validate requested pins exist in capture
        for label, pin in [("clk_pin", clk_pin), ("mosi_pin", mosi_pin)]:
            if pin not in captured_pins:
                return {"error": f"{label}={pin!r} was not captured; available: {captured_pins}"}
        if miso_pin and miso_pin not in captured_pins:
            return {"error": f"miso_pin={miso_pin!r} was not captured; available: {captured_pins}"}
        if cs_pin and cs_pin not in captured_pins:
            return {"error": f"cs_pin={cs_pin!r} was not captured; available: {captured_pins}"}

        # Build (n_samples, 16) array from npz columns
        data = np.load(npz_path)
        n = len(data[captured_pins[0]])
        samples = np.zeros((n, 16), dtype=np.uint8)
        for pin in captured_pins:
            col = int(pin[3:])  # "dio3" → 3
            samples[:, col] = data[pin]

        pin_map: dict[str, int] = {
            "clk": int(clk_pin[3:]),
            "mosi": int(mosi_pin[3:]),
        }
        if miso_pin:
            pin_map["miso"] = int(miso_pin[3:])
        if cs_pin:
            pin_map["cs"] = int(cs_pin[3:])

        decoder = SpiDecoder()
        txns = decoder.decode(
            samples, pin_map,
            sample_rate_hz=float(sample_rate_hz),
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
                    "sample_rate_hz": sample_rate_hz,
                },
                output_path=output_path,
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

    def release(self) -> None:
        pass
