"""Scope (analog-in) instrument. Buffer-mode acquisition for v1; streaming deferred."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_VALID_COUPLINGS = {"DC", "AC"}
_VALID_CONDITIONS = {"Rising", "Falling", "Either"}

SCOPE_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channels", "range_v", "sample_rate_hz", "buffer_size"],
    "properties": {
        "channels": {
            "type": "array",
            "items": {"type": "integer", "enum": [1, 2]},
            "minItems": 1,
            "uniqueItems": True,
        },
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0},
        "offset_v": {"type": "number", "default": 0.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "buffer_size": {"type": "integer", "minimum": 16, "maximum": 1_048_576},
    },
}

SCOPE_TRIGGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source"],
    "properties": {
        "source": {
            "type": "string",
            "enum": ["none", "detector_analog_in", "external1", "external2"],
        },
        "channel": {"type": "integer", "enum": [1, 2]},
        "level_v": {"type": "number", "default": 0.0},
        "condition": {
            "type": "string",
            "enum": ["Rising", "Falling", "Either"],
            "default": "Rising",
        },
        "position_s": {"type": "number", "default": 0.0},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}

SCOPE_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_path": {"type": "string"},
        "description": {"type": "string"},
    },
}


class Scope(Instrument):
    name = "scope"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":   ("configure",   SCOPE_CONFIGURE_SCHEMA),
        "set_trigger": ("set_trigger", SCOPE_TRIGGER_SCHEMA),
        "capture":     ("capture",     SCOPE_CAPTURE_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None
        self._trigger: dict[str, Any] | None = None

    def configure(
        self,
        channels: list[int],
        range_v: float,
        sample_rate_hz: float,
        buffer_size: int,
        offset_v: float = 0.0,
        coupling: str = "DC",
    ) -> dict[str, Any]:
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(
                f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}"
            )
        pin_names = [f"scope{c}" for c in channels]
        self.device.allocator.claim("scope", pin_names)
        # Clear stale state BEFORE backend calls so a partial failure leaves the
        # instrument in an unconfigured state rather than an inconsistent one.
        self._config = None
        self._trigger = None
        try:
            for ch in (1, 2):
                self.device.backend.scope_configure(
                    channel=ch,
                    range_v=range_v,
                    offset_v=offset_v,
                    coupling=coupling,
                    enable=(ch in channels),
                )
            self.device.backend.scope_set_acquisition(
                sample_rate_hz=sample_rate_hz,
                buffer_size=buffer_size,
                mode="Single",
            )
        except Exception:
            self.device.allocator.release("scope")
            raise
        self._config = {
            "channels": list(channels),
            "range_v": range_v,
            "offset_v": offset_v,
            "coupling": coupling,
            "sample_rate_hz": sample_rate_hz,
            "buffer_size": buffer_size,
        }
        return {"configured": True}

    def set_trigger(
        self,
        source: str,
        channel: int | None = None,
        level_v: float = 0.0,
        condition: str = "Rising",
        position_s: float = 0.0,
        timeout_s: float = 1.0,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured(
                "scope.configure must be called before set_trigger"
            )
        if condition not in _VALID_CONDITIONS:
            raise ValueError(
                f"condition must be one of {sorted(_VALID_CONDITIONS)}, got {condition!r}"
            )
        self.device.backend.scope_set_trigger(
            source=source,
            channel=channel,
            level_v=level_v,
            condition=condition,
            position_s=position_s,
            timeout_s=timeout_s,
        )
        self._trigger = {
            "source": source,
            "channel": channel,
            "level_v": level_v,
            "condition": condition,
            "position_s": position_s,
            "timeout_s": timeout_s,
        }
        return {"trigger_set": True}

    def capture(
        self,
        output_path: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured(
                "scope.configure must be called before capture"
            )
        cfg = self._config
        self.device.backend.scope_arm()
        deadline = time.monotonic() + max(
            cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0
        )
        while time.monotonic() < deadline:
            if self.device.backend.scope_status() == "Done":
                break
        else:
            raise RuntimeError("scope capture did not complete before deadline")

        arrays: dict[str, np.ndarray[Any, Any]] = {}
        summary_per_ch: dict[str, dict[str, float]] = {}
        for ch in cfg["channels"]:
            samples = self.device.backend.scope_read(
                channel=ch, count=cfg["buffer_size"]
            )
            arrays[f"ch{ch}"] = samples
            summary_per_ch[f"ch{ch}"] = self._summarize(samples, cfg["sample_rate_hz"])

        summary = CaptureSummary(
            instrument="scope",
            sample_count=cfg["buffer_size"],
            sample_rate_hz=cfg["sample_rate_hz"],
            extra=summary_per_ch,
        )
        sidecar_config = {**cfg, "trigger": self._trigger}
        result = self.artifacts.write_npz(
            instrument="scope",
            arrays=arrays,
            config=sidecar_config,
            summary=summary,
            output_path=Path(output_path) if output_path else None,
            description=description,
        )
        return {
            "path": result.path,
            "sidecar_path": result.sidecar_path,
            "summary": summary_per_ch,
        }

    def release(self) -> None:
        self.device.allocator.release("scope")
        self._config = None
        self._trigger = None

    @staticmethod
    def _summarize(
        samples: np.ndarray[Any, Any], sample_rate_hz: float
    ) -> dict[str, float]:
        arr = np.asarray(samples, dtype=np.float64)
        if len(arr) == 0:
            return {
                "min": 0.0, "max": 0.0, "mean": 0.0, "rms": 0.0,
                "freq_estimate": 0.0, "sample_rate": sample_rate_hz,
            }
        mean = float(arr.mean())
        rms = float(np.sqrt(np.mean(arr**2)))
        # Rough frequency estimate via zero-crossings about the signal midpoint
        # (max+min)/2 — robust to DC bias and partial-cycle mean drift that would
        # add a phantom crossing if we centered by the arithmetic mean.
        if len(arr) > 0:
            midpoint = float(arr.max() + arr.min()) / 2.0
            centered = arr - midpoint
            signs = np.signbit(centered)
            crossings = int(np.sum(signs[:-1] != signs[1:]))
            freq_estimate = (crossings / 2.0) * (sample_rate_hz / len(arr))
        else:
            freq_estimate = 0.0
        return {
            "min": float(arr.min()),
            "max": float(arr.max()),
            "mean": mean,
            "rms": rms,
            "freq_estimate": freq_estimate,
            "sample_rate": sample_rate_hz,
        }
