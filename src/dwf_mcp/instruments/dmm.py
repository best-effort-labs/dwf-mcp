from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument

log = logging.getLogger(__name__)

_VALID_COUPLINGS = {"DC", "AC"}

DMM_MEASURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "range_v"],
    "properties": {
        "channel": {"type": "integer", "enum": [1, 2]},
        "range_v": {"type": "number", "minimum": 0.001, "maximum": 50.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "n_averages": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 64},
    },
}


class DMM(Instrument):
    name = "dmm"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "measure": ("measure", DMM_MEASURE_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts

    def measure(
        self,
        channel: int,
        range_v: float,
        coupling: str = "DC",
        n_averages: int = 64,
    ) -> dict[str, Any]:
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(
                f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}"
            )
        self.device.allocator.claim("dmm", ["scope1", "scope2"])
        try:
            self.device.backend.dmm_configure(channel, range_v, coupling, n_averages)
            self.device.backend.dmm_arm()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if self.device.backend.dmm_status() == "Done":
                    break
                time.sleep(0.002)  # yield the core instead of busy-waiting
            else:
                raise RuntimeError("DMM acquisition timed out after 2s")
            samples = self.device.backend.dmm_read(channel, n_averages)
        finally:
            try:
                self.device.backend.dmm_stop()
            except Exception as exc:
                log.warning("dmm_stop failed: %s", exc)
            self.device.allocator.release("dmm")
        arr = np.asarray(samples, dtype=np.float64)
        return {
            "channel": channel,
            "mean_v": float(arr.mean()),
            "min_v": float(arr.min()),
            "max_v": float(arr.max()),
            "rms_v": float(np.sqrt(np.mean(arr**2))),
            "range_v": range_v,
            "coupling": coupling,
        }

    def release(self) -> None:
        self.device.allocator.release("dmm")
