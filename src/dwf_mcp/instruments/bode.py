"""Bode / network analyzer instrument. measure() sweeps an AWG sine and measures
gain/phase ratiometrically from two analog-in channels — coherent-first capture,
hardware readback of actual freq/rate, explicit per-point quality flags. Drives
the AWG + scope backend paths directly under one "bode" allocator claim."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.bode_dsp import (
    QF_CLIPPED,
    QF_LOW_VIN_RMS,
    QF_NAMES,
    assess_quality,
    bode_point,
    detect_clip,
    frequency_grid,
    plan_acquisition,
)
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

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
        be = self.device.backend
        drive, ref, dut = cfg["drive_channel"], cfg["ref_channel"], cfg["dut_channel"]
        n_ch = info.analog_in_channels
        pins = [f"awg{drive}"] + [f"scope{i}" for i in range(1, n_ch + 1)]
        freqs = frequency_grid(cfg["start_hz"], cfg["stop_hz"], cfg["points"], cfg["spacing"])
        cols: dict[str, list[float]] = {k: [] for k in (
            "frequency_hz", "gain_db", "phase_deg", "vin_rms", "vout_rms",
            "achieved_cycles", "samples_per_cycle", "coherence_error_cycles",
            "quality_flags", "clipping_flag",
        )}
        sr_seen: list[float] = []
        buf_seen: list[int] = []
        self.device.allocator.claim("bode", pins)
        try:
            # Gate once for the whole sweep: amplitude is constant across all points, so
            # one authorization covers every per-point awg_start (which call the backend
            # directly to avoid per-frequency gate/log overhead during a long sweep).
            self.device.gate_output("awg_start", channel=drive, amplitude=cfg["amplitude_v"])
            self._warm_up(be, ref, dut, n_ch)
            for f_req in freqs:
                self._point(be, info, float(f_req), cfg, ref, dut, n_ch, cols, sr_seen, buf_seen)
        finally:
            try:
                be.awg_stop(channel=drive)
            finally:
                self.device.allocator.release("bode")
        return self._write(cols, cfg, sr_seen, buf_seen, output_path, description)

    def _point(
        self,
        be: Any,
        info: Any,
        f_req: float,
        cfg: dict[str, Any],
        ref: int,
        dut: int,
        n_ch: int,
        cols: dict[str, list[float]],
        sr_seen: list[float],
        buf_seen: list[int],
    ) -> None:
        be.awg_configure(
            channel=cfg["drive_channel"],
            function="Sine",
            freq_hz=f_req,
            amplitude_v=cfg["amplitude_v"],
            offset_v=0.0,
            phase_deg=0.0,
            symmetry=50.0,
            run_time_s=None,
        )
        be.awg_start(channel=cfg["drive_channel"])
        f_act = be.awg_frequency_get(cfg["drive_channel"]) or f_req
        plan = plan_acquisition(
            f_act,
            info.sample_rate_max_hz or 0.0,
            info.analog_in_buffer_max or 0,
            samples_per_cycle=cfg["samples_per_cycle"],
            min_cycles=cfg["min_cycles"],
        )
        for c in range(1, n_ch + 1):
            be.scope_configure(
                channel=c,
                range_v=cfg["range_v"],
                offset_v=0.0,
                coupling="DC",
                enable=(c in (ref, dut)),
            )
        be.scope_set_acquisition(
            sample_rate_hz=plan.sample_rate_hz,
            buffer_size=plan.buffer_size,
            mode="Single",
        )
        be.scope_arm()
        sr_act = be.scope_sample_rate_get() or plan.sample_rate_hz
        self._settle(f_act, cfg)
        self._await_done(plan)
        vin = np.asarray(be.scope_read(channel=ref, count=plan.buffer_size))
        vout = np.asarray(be.scope_read(channel=dut, count=plan.buffer_size))
        n = plan.buffer_size
        achieved_cycles = f_act * n / sr_act if sr_act else 0.0
        spc = sr_act / f_act if f_act else 0.0
        qflags, coh_err = assess_quality(
            achieved_cycles, spc, f_act, sr_act, cfg["min_cycles"]
        )
        qflags |= plan.clamp_flags
        p = bode_point(vin, vout, sr_act, f_act)
        if p["vin_rms"] < cfg["min_vin_rms"]:
            qflags |= QF_LOW_VIN_RMS
        if detect_clip(vin, cfg["range_v"]) or detect_clip(vout, cfg["range_v"]):
            qflags |= QF_CLIPPED
        cols["frequency_hz"].append(f_act)
        cols["gain_db"].append(p["gain_db"])
        cols["phase_deg"].append(p["phase_deg"])
        cols["vin_rms"].append(p["vin_rms"])
        cols["vout_rms"].append(p["vout_rms"])
        cols["achieved_cycles"].append(achieved_cycles)
        cols["samples_per_cycle"].append(spc)
        cols["coherence_error_cycles"].append(coh_err)
        cols["quality_flags"].append(int(qflags))
        cols["clipping_flag"].append(int(bool(qflags & QF_CLIPPED)))
        sr_seen.append(sr_act)
        buf_seen.append(n)

    def _warm_up(self, be: Any, ref: int, dut: int, n_ch: int) -> None:
        """One throwaway capture: the first AnalogIn acquisition after open is stale.
        Fired unconditionally on every measure() (a sweep is heavyweight, so an
        always-flush is strictly safer than per-open tracking at negligible cost).
        One scope_arm captures all enabled channels in the same cycle, so a single
        scope_read is enough to discard the stale acquisition for both ref and dut."""
        for c in range(1, n_ch + 1):
            be.scope_configure(
                channel=c,
                range_v=5.0,
                offset_v=0.0,
                coupling="DC",
                enable=(c in (ref, dut)),
            )
        be.scope_set_acquisition(sample_rate_hz=100_000.0, buffer_size=1024, mode="Single")
        be.scope_arm()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and be.scope_status() != "Done":
            time.sleep(0.002)
        be.scope_read(channel=ref, count=1024)

    def _settle(self, freq_hz: float, cfg: dict[str, Any]) -> None:
        if cfg["settle_s"] is not None:
            base = cfg["settle_s"]
        else:
            base = cfg["settle_cycles"] / freq_hz if freq_hz else 0.0
        delay = max(base, cfg["settle_min_s"])
        if delay > 0:
            time.sleep(delay)

    def _await_done(self, plan: Any) -> None:
        deadline = time.monotonic() + max(
            plan.buffer_size / plan.sample_rate_hz * 10 + 1.0, 2.0
        )
        while time.monotonic() < deadline:
            if self.device.backend.scope_status() == "Done":
                return
            time.sleep(0.002)
        raise RuntimeError("bode capture did not complete before deadline")

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
