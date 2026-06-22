"""Bode / network analyzer instrument. measure() sweeps an AWG sine and measures
gain/phase ratiometrically from two analog-in channels — coherent-first capture,
hardware readback of actual freq/rate, explicit per-point quality flags. Drives
the AWG + scope backend paths directly under one "bode" allocator claim."""
from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.bode_dsp import (
    QF_CLIPPED,
    QF_LOW_VIN_RMS,
    QF_NAMES,
    bode_point,
    detect_clip,
)
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.instruments._sweep import SweepPoint, run_sweep

_SPACINGS = ["log", "linear"]

BODE_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["start_hz", "stop_hz", "points"],
    "properties": {
        "start_hz": {"type": "number", "minimum": 0.0},
        "stop_hz": {"type": "number", "minimum": 0.0},
        "points": {"type": "integer", "minimum": 2},
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
        "min_vin_rms": {"type": "number", "minimum": 0.0, "default": 0.01},
    },
}
BODE_MEASURE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

_BODE_COLUMNS = (
    "frequency_hz", "gain_db", "phase_deg", "vin_rms", "vout_rms",
    "achieved_cycles", "samples_per_cycle", "coherence_error_cycles",
    "quality_flags", "clipping_flag",
)


class Bode(Instrument):
    name = "bode"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", BODE_CONFIGURE_SCHEMA),
        "measure":   ("measure",   BODE_MEASURE_SCHEMA),
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
        min_vin_rms: float = 0.01,
    ) -> dict[str, Any]:
        self.device.require_open()
        self.device.validate_channel(drive_channel, "awg")
        self.device.validate_channel(ref_channel, "scope")
        self.device.validate_channel(dut_channel, "scope")
        if ref_channel == dut_channel:
            raise ValueError(
                f"ref_channel and dut_channel must differ (both {ref_channel})"
            )
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
            "min_vin_rms": min_vin_rms,
        }
        return {"configured": True, **self._config}

    def measure(
        self,
        output_path: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("bode.configure must be called before measure")
        cfg = self._config
        info = self.device.require_open()
        cols, sr_seen, buf_seen = run_sweep(
            self.device, info, instrument_name="bode", cfg=cfg,
            column_keys=_BODE_COLUMNS, record_point=self._record_point,
        )
        return self._write(cols, cfg, sr_seen, buf_seen, output_path, description)

    def _record_point(self, pt: SweepPoint, cols: dict[str, list[float]]) -> None:
        cfg = self._config
        assert cfg is not None
        qflags = pt.qflags
        p = bode_point(pt.vin, pt.vout, pt.sr_act, pt.f_act)
        if p["vin_rms"] < cfg["min_vin_rms"]:
            qflags |= QF_LOW_VIN_RMS
        if detect_clip(pt.vin, cfg["range_v"]) or detect_clip(pt.vout, cfg["range_v"]):
            qflags |= QF_CLIPPED
        cols["frequency_hz"].append(pt.f_act)
        cols["gain_db"].append(p["gain_db"])
        cols["phase_deg"].append(p["phase_deg"])
        cols["vin_rms"].append(p["vin_rms"])
        cols["vout_rms"].append(p["vout_rms"])
        cols["achieved_cycles"].append(pt.achieved_cycles)
        cols["samples_per_cycle"].append(pt.spc)
        cols["coherence_error_cycles"].append(pt.coh_err)
        cols["quality_flags"].append(int(qflags))
        cols["clipping_flag"].append(int(bool(qflags & QF_CLIPPED)))

    def _write(
        self,
        cols: dict[str, list[float]],
        cfg: dict[str, Any],
        sr_seen: list[float],
        buf_seen: list[int],
        output_path: str | None,
        description: str | None,
    ) -> dict[str, Any]:
        arrays = {
            "frequency_hz": np.asarray(cols["frequency_hz"], dtype=np.float64),
            "gain_db": np.asarray(cols["gain_db"], dtype=np.float64),
            "phase_deg": np.asarray(cols["phase_deg"], dtype=np.float64),
            "vin_rms": np.asarray(cols["vin_rms"], dtype=np.float64),
            "vout_rms": np.asarray(cols["vout_rms"], dtype=np.float64),
            "achieved_cycles": np.asarray(cols["achieved_cycles"], dtype=np.float64),
            "samples_per_cycle": np.asarray(cols["samples_per_cycle"], dtype=np.float64),
            "coherence_error_cycles": np.asarray(
                cols["coherence_error_cycles"], dtype=np.float64
            ),
            "quality_flags": np.asarray(cols["quality_flags"], dtype=np.int64),
            "clipping_flag": np.asarray(cols["clipping_flag"], dtype=np.int64),
        }
        gains = arrays["gain_db"]
        phases = arrays["phase_deg"]
        freqs = arrays["frequency_hz"]
        summary_extra = {
            "point_count": int(freqs.size),
            "start_hz": float(freqs[0]) if freqs.size else 0.0,
            "stop_hz": float(freqs[-1]) if freqs.size else 0.0,
            "gain_db_min": float(gains.min()) if gains.size else 0.0,
            "gain_db_max": float(gains.max()) if gains.size else 0.0,
            "phase_deg_min": float(phases.min()) if phases.size else 0.0,
            "phase_deg_max": float(phases.max()) if phases.size else 0.0,
            "flagged_points": int(np.count_nonzero(arrays["quality_flags"])),
            "clipped_points": int(np.count_nonzero(arrays["clipping_flag"])),
        }
        config = {
            **cfg,
            "quality_flags_bits": {v: k for k, v in QF_NAMES.items()},
            "actual_sample_rate_min_hz": float(min(sr_seen)) if sr_seen else 0.0,
            "actual_sample_rate_max_hz": float(max(sr_seen)) if sr_seen else 0.0,
            "buffer_size_min": int(min(buf_seen)) if buf_seen else 0,
            "buffer_size_max": int(max(buf_seen)) if buf_seen else 0,
        }
        summary = CaptureSummary(
            instrument="bode",
            sample_count=int(freqs.size),
            sample_rate_hz=None,
            extra=summary_extra,
        )
        res = self.artifacts.write_npz(
            instrument="bode",
            arrays=arrays,
            config=config,
            summary=summary,
            output_path=Path(output_path) if output_path else None,
            description=description,
        )
        return {"path": res.path, "sidecar_path": res.sidecar_path, "summary": summary_extra}

    def release(self) -> None:
        self.device.allocator.release("bode")
        self._config = None
