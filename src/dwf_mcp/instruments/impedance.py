"""Impedance analyzer instrument. measure() sweeps an AWG sine across a series
reference resistor (W1 -> R_ref -> DUT -> GND) and recovers complex impedance Z(f)
ratiometrically from CH1 (V_total) and CH2 (V_dut) — coherent-first capture, hardware
readback of actual freq/rate, explicit per-point quality flags. Clones the Bode
orchestration under one "impedance" allocator claim."""
from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_SPACINGS = ["log", "linear"]

IMPEDANCE_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["start_hz", "stop_hz", "points", "r_ref"],
    "properties": {
        "start_hz": {"type": "number", "minimum": 0.0},
        "stop_hz": {"type": "number", "minimum": 0.0},
        "points": {"type": "integer", "minimum": 2},
        "r_ref": {"type": "number", "exclusiveMinimum": 0.0},
        "spacing": {"type": "string", "enum": _SPACINGS, "default": "log"},
        "amplitude_v": {"type": "number", "minimum": 0.0, "default": 0.5},
        "drive_channel": {"type": "integer", "minimum": 1, "default": 1},
        "ref_channel": {"type": "integer", "minimum": 1, "default": 1},
        "dut_channel": {"type": "integer", "minimum": 1, "default": 2},
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0, "default": 5.0},
        "samples_per_cycle": {"type": "number", "minimum": 4.0, "default": 64.0},
        "min_cycles": {"type": "integer", "minimum": 1, "default": 16},
        "settle_cycles": {"type": "number", "minimum": 0.0, "default": 4.0},
        "settle_min_s": {"type": "number", "minimum": 0.0, "default": 0.001},
        "settle_s": {"type": "number", "minimum": 0.0},
        "min_drive_rms": {"type": "number", "minimum": 0.0, "default": 0.01},
        "min_dut_rms": {"type": "number", "minimum": 0.0, "default": 0.01},
    },
}
IMPEDANCE_MEASURE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class Impedance(Instrument):
    name = "impedance"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", IMPEDANCE_CONFIGURE_SCHEMA),
        "measure":   ("measure",   IMPEDANCE_MEASURE_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None

    def configure(
        self,
        start_hz: float,
        stop_hz: float,
        points: int,
        r_ref: float,
        spacing: str = "log",
        amplitude_v: float = 0.5,
        drive_channel: int = 1,
        ref_channel: int = 1,
        dut_channel: int = 2,
        range_v: float = 5.0,
        samples_per_cycle: float = 64.0,
        min_cycles: int = 16,
        settle_cycles: float = 4.0,
        settle_min_s: float = 0.001,
        settle_s: float | None = None,
        min_drive_rms: float = 0.01,
        min_dut_rms: float = 0.01,
    ) -> dict[str, Any]:
        self.device.require_open()
        self.device.validate_channel(drive_channel, "awg")
        self.device.validate_channel(ref_channel, "scope")
        self.device.validate_channel(dut_channel, "scope")
        if ref_channel == dut_channel:
            raise ValueError(
                f"ref_channel and dut_channel must differ (both {ref_channel})"
            )
        if r_ref <= 0:
            raise ValueError(f"r_ref must be > 0, got {r_ref}")
        if spacing not in _SPACINGS:
            raise ValueError(f"spacing must be one of {_SPACINGS}, got {spacing!r}")
        if start_hz <= 0:
            raise ValueError(f"start_hz must be > 0, got {start_hz}")
        if not (start_hz < stop_hz):
            raise ValueError(f"start_hz ({start_hz}) must be < stop_hz ({stop_hz})")
        if points < 2:
            raise ValueError(f"points must be >= 2, got {points}")
        self._config = {
            "start_hz": start_hz,
            "stop_hz": stop_hz,
            "points": points,
            "r_ref": r_ref,
            "spacing": spacing,
            "amplitude_v": amplitude_v,
            "drive_channel": drive_channel,
            "ref_channel": ref_channel,
            "dut_channel": dut_channel,
            "range_v": range_v,
            "samples_per_cycle": samples_per_cycle,
            "min_cycles": min_cycles,
            "settle_cycles": settle_cycles,
            "settle_min_s": settle_min_s,
            "settle_s": settle_s,
            "min_drive_rms": min_drive_rms,
            "min_dut_rms": min_dut_rms,
        }
        return {"configured": True, **self._config}

    def measure(
        self,
        output_path: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured(
                "impedance.configure must be called before measure"
            )
        raise NotImplementedError  # implemented in Task 5

    def release(self) -> None:
        self.device.allocator.release("impedance")
        self._config = None
