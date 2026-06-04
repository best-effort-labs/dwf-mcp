"""Logic (DigitalIn) instrument: buffer-mode capture and streaming record."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp import vcd_writer
from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

log = logging.getLogger(__name__)

_VALID_SOURCES = frozenset({"none", "detector_digital_in", "external1", "external2"})
_VALID_FORMATS = frozenset({"npz", "vcd"})


def _pins_to_mask(pins: list[str]) -> int:
    mask = 0
    for p in pins:
        mask |= 1 << int(p[3:])
    return mask


def _pin_indices(pins: list[str]) -> list[int]:
    return [int(p[3:]) for p in pins]


LOGIC_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pins", "sample_rate_hz", "buffer_size"],
    "properties": {
        "pins": {
            "type": "array",
            "items": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
            "minItems": 1,
            "uniqueItems": True,
        },
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "buffer_size": {"type": "integer", "minimum": 16, "maximum": 1_048_576},
    },
}

LOGIC_TRIGGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source"],
    "properties": {
        "source": {"type": "string", "enum": sorted(_VALID_SOURCES)},
        "pin": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
        "level": {"type": "number"},
        "condition": {"type": "string", "enum": ["Rising", "Falling", "Either"]},
        "position_s": {"type": "number", "default": 0.0},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}

LOGIC_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_path": {"type": "string"},
        "format": {"type": "string", "enum": ["npz", "vcd"], "default": "npz"},
    },
}

LOGIC_RECORD_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pins", "sample_rate_hz", "duration_s"],
    "properties": {
        "pins": {
            "type": "array",
            "items": {"type": "string", "pattern": "^dio([0-9]|1[0-5])$"},
            "minItems": 1,
            "uniqueItems": True,
        },
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "duration_s": {"type": "number", "minimum": 0.001},
        "output_path": {"type": "string"},
        "format": {"type": "string", "enum": ["npz", "vcd"], "default": "npz"},
    },
}

LOGIC_RECORD_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["record_id"],
    "properties": {"record_id": {"type": "string"}},
}


@dataclasses.dataclass
class _RecordingSession:
    record_id: str
    task: asyncio.Task[Any]
    queue: asyncio.Queue[Any]  # streaming seam for future MCP notifications
    chunks: list[np.ndarray]
    pins: list[str]
    sample_rate_hz: float
    output_path: str | None
    format: str
    lost_samples: int
    done: bool
    error: str | None


class Logic(Instrument):
    name = "logic"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":     ("configure",     LOGIC_CONFIGURE_SCHEMA),
        "set_trigger":   ("set_trigger",   LOGIC_TRIGGER_SCHEMA),
        "capture":       ("capture",       LOGIC_CAPTURE_SCHEMA),
        "record_start":  ("record_start",  LOGIC_RECORD_START_SCHEMA),
        "record_status": ("record_status", LOGIC_RECORD_ID_SCHEMA),
        "record_stop":   ("record_stop",   LOGIC_RECORD_ID_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None
        self._sessions: dict[str, _RecordingSession] = {}

    # --- Buffer-mode ---

    def configure(
        self,
        pins: list[str],
        sample_rate_hz: float,
        buffer_size: int,
    ) -> dict[str, Any]:
        self.device.allocator.claim("logic", pins)
        self._config = None
        try:
            self.device.backend.logic_configure(
                pin_mask=_pins_to_mask(pins),
                sample_rate_hz=sample_rate_hz,
                buffer_size=buffer_size,
            )
        except Exception:
            self.device.allocator.release("logic")
            raise
        self._config = {
            "pins": list(pins),
            "sample_rate_hz": sample_rate_hz,
            "buffer_size": buffer_size,
        }
        return {"configured": True, "pins": pins}

    def set_trigger(
        self,
        source: str,
        pin: str | None = None,
        level: float | None = None,
        condition: str | None = None,
        position_s: float = 0.0,
        timeout_s: float = 1.0,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("logic.configure must be called before set_trigger")
        pin_idx = int(pin[3:]) if pin else None
        self.device.backend.logic_set_trigger(
            source=source,
            pin_idx=pin_idx,
            level=level,
            condition=condition,
            position_s=position_s,
            timeout_s=timeout_s,
        )
        return {"trigger_set": True}

    def capture(
        self,
        output_path: str | None = None,
        format: str = "npz",
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("logic.configure must be called before capture")
        if format not in _VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(_VALID_FORMATS)}, got {format!r}")
        if format == "vcd" and not vcd_writer.HAS_VCD:
            raise ImportError(
                "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
            )
        cfg = self._config
        self.device.backend.logic_arm()
        deadline = time.monotonic() + max(
            cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0
        )
        while time.monotonic() < deadline:
            if self.device.backend.logic_status() == "Done":
                break
        else:
            raise RuntimeError("logic capture did not complete before deadline")

        raw = self.device.backend.logic_read(count=cfg["buffer_size"])
        pin_indices = _pin_indices(cfg["pins"])
        samples = raw[:, pin_indices].astype(np.uint8)

        return self._write_artifact(
            samples=samples,
            pin_names=cfg["pins"],
            sample_rate_hz=cfg["sample_rate_hz"],
            output_path=output_path,
            format=format,
        )

    def _write_artifact(
        self,
        samples: np.ndarray,
        pin_names: list[str],
        sample_rate_hz: float,
        output_path: str | None,
        format: str,
    ) -> dict[str, Any]:
        if format == "vcd":
            path = Path(output_path) if output_path else (
                self.artifacts.workspace / "captures" / f"logic_{uuid.uuid4().hex[:8]}.vcd"
            )
            vcd_writer.write(path, samples, pin_names, sample_rate_hz)
            return {"path": str(path), "format": "vcd", "n_samples": samples.shape[0]}

        arrays = {name: samples[:, i] for i, name in enumerate(pin_names)}
        summary = CaptureSummary(
            instrument="logic",
            sample_count=len(samples),
            sample_rate_hz=sample_rate_hz,
        )
        result = self.artifacts.write_npz(
            instrument="logic",
            arrays=arrays,
            config={"pins": pin_names, "sample_rate_hz": sample_rate_hz},
            summary=summary,
            output_path=Path(output_path) if output_path else None,
        )
        return {"path": result.path, "sidecar_path": result.sidecar_path, "format": "npz", "n_samples": samples.shape[0]}

    # --- Streaming stubs (implemented in Task 7) ---

    async def record_start(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("record_start is implemented in Task 7")

    def record_status(self, record_id: str) -> dict[str, Any]:
        raise NotImplementedError("record_status is implemented in Task 7")

    async def record_stop(self, record_id: str) -> dict[str, Any]:
        raise NotImplementedError("record_stop is implemented in Task 7")

    def release(self) -> None:
        for session in list(self._sessions.values()):
            session.task.cancel()
        self._sessions.clear()
        self.device.allocator.release("logic")
        self._config = None
