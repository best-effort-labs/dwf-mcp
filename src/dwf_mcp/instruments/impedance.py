"""Impedance analyzer instrument. measure() sweeps an AWG sine across a series
reference resistor (W1 -> R_ref -> DUT -> GND) and recovers complex impedance Z(f)
ratiometrically from CH1 (V_total) and CH2 (V_dut) — coherent-first capture, hardware
readback of actual freq/rate, explicit per-point quality flags. Clones the Bode
orchestration under one "impedance" allocator claim."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.impedance_dsp import impedance_point
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.sweep_dsp import (
    QF_CLIPPED,
    QF_LOW_DRIVE,
    QF_LOW_DUT_VOLTAGE,
    QF_NAMES,
    QF_REF_MISMATCH,
    assess_quality,
    detect_clip,
    frequency_grid,
    plan_acquisition,
)

_SPACINGS = ["log", "linear"]

# Ref-mismatch guard bounds: |Z|/R_ref must be within [_REF_MISMATCH_LO, _REF_MISMATCH_HI]
_REF_MISMATCH_LO = 0.01
_REF_MISMATCH_HI = 100.0

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
        cfg = self._config
        info = self.device.require_open()
        be = self.device.backend
        drive, ref, dut = cfg["drive_channel"], cfg["ref_channel"], cfg["dut_channel"]
        n_ch = info.analog_in_channels
        pins = [f"awg{drive}"] + [f"scope{i}" for i in range(1, n_ch + 1)]
        freqs = frequency_grid(cfg["start_hz"], cfg["stop_hz"], cfg["points"], cfg["spacing"])
        cols: dict[str, list[float]] = {k: [] for k in (
            "frequency_hz", "impedance_ohms", "phase_deg", "resistance_ohms",
            "reactance_ohms", "capacitance_f", "inductance_f", "q_factor", "dissipation",
            "v_total_rms", "v_dut_rms", "achieved_cycles", "samples_per_cycle",
            "coherence_error_cycles", "quality_flags", "clipping_flag",
        )}
        sr_seen: list[float] = []
        buf_seen: list[int] = []
        self.device.allocator.claim("impedance", pins)
        try:
            # Gate once for the whole sweep: amplitude is constant across all points, so
            # one authorization covers every per-point awg_start (which call the backend
            # directly to avoid per-frequency gate/log overhead during a long sweep).
            self.device.gate_output("awg_start", channel=drive, amplitude=cfg["amplitude_v"])
            # Clear any trigger left configured by a prior scope use: impedance free-runs
            # each capture (settle, then arm), so a stale edge trigger would stall
            # acquisition until the auto-timeout or capture at a trigger-dependent phase.
            be.scope_set_trigger(source="none", channel=None, level_v=0.0,
                                 condition="Either", position_s=0.0, timeout_s=0.0)
            self._warm_up(be, ref, dut, n_ch)
            for f_req in freqs:
                self._point(be, info, float(f_req), cfg, ref, dut, n_ch, cols, sr_seen, buf_seen)
        finally:
            try:
                be.awg_stop(channel=drive)
            finally:
                self.device.allocator.release("impedance")
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
        # Actuals come from hardware readbacks ONLY — never fall back to the requested/
        # planned values: a soft-failed readback (0/garbage) would make the capture look
        # coherent-by-construction and suppress the noncoherent flag (silent garbage).
        f_act = be.awg_frequency_get(cfg["drive_channel"])
        if not f_act > 0:
            raise RuntimeError(
                f"AWG frequency readback returned {f_act!r} (expected > 0) at "
                f"requested {f_req} Hz")
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
        sr_act = be.scope_sample_rate_get()
        if not sr_act > 0:
            raise RuntimeError(
                f"scope sample-rate readback returned {sr_act!r} (expected > 0)")
        # Settle the AWG/DUT to steady state BEFORE arming: scope_arm() starts the single
        # acquisition, so settling after the arm would capture the post-retune transient.
        self._settle(f_act, cfg)
        be.scope_arm()
        self._await_done(plan)
        v_total = np.asarray(be.scope_read(channel=ref, count=plan.buffer_size))
        v_dut = np.asarray(be.scope_read(channel=dut, count=plan.buffer_size))
        n = plan.buffer_size
        achieved_cycles = f_act * n / sr_act
        spc = sr_act / f_act
        qflags, coh_err = assess_quality(
            achieved_cycles, spc, f_act, sr_act, cfg["min_cycles"]
        )
        qflags |= plan.clamp_flags
        p = impedance_point(v_total, v_dut, sr_act, f_act, cfg["r_ref"])
        if p["drive_rms"] < cfg["min_drive_rms"]:
            qflags |= QF_LOW_DRIVE
        if p["v_dut_rms"] < cfg["min_dut_rms"]:
            qflags |= QF_LOW_DUT_VOLTAGE
        zmag = p["impedance_ohms"]
        if np.isfinite(zmag) and (zmag < _REF_MISMATCH_LO * cfg["r_ref"]
                                  or zmag > _REF_MISMATCH_HI * cfg["r_ref"]):
            qflags |= QF_REF_MISMATCH
        if detect_clip(v_total, cfg["range_v"]) or detect_clip(v_dut, cfg["range_v"]):
            qflags |= QF_CLIPPED
        cols["frequency_hz"].append(f_act)
        for key in ("impedance_ohms", "phase_deg", "resistance_ohms", "reactance_ohms",
                    "capacitance_f", "inductance_f", "q_factor", "dissipation",
                    "v_total_rms", "v_dut_rms"):
            cols[key].append(p[key])
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
        raise RuntimeError("impedance capture did not complete before deadline")

    def _write(
        self,
        cols: dict[str, list[float]],
        cfg: dict[str, Any],
        sr_seen: list[float],
        buf_seen: list[int],
        output_path: str | None,
        description: str | None,
    ) -> dict[str, Any]:
        float_keys = (
            "frequency_hz", "impedance_ohms", "phase_deg", "resistance_ohms",
            "reactance_ohms", "capacitance_f", "inductance_f", "q_factor",
            "dissipation", "v_total_rms", "v_dut_rms", "achieved_cycles",
            "samples_per_cycle", "coherence_error_cycles",
        )
        arrays: dict[str, np.ndarray] = {
            k: np.asarray(cols[k], dtype=np.float64) for k in float_keys
        }
        arrays["quality_flags"] = np.asarray(cols["quality_flags"], dtype=np.int64)
        arrays["clipping_flag"] = np.asarray(cols["clipping_flag"], dtype=np.int64)
        z = arrays["impedance_ohms"]
        freqs = arrays["frequency_hz"]
        z_has_finite = bool(z.size) and bool(np.any(np.isfinite(z)))
        summary_extra = {
            "point_count": int(freqs.size),
            "start_hz": float(freqs[0]) if freqs.size else 0.0,
            "stop_hz": float(freqs[-1]) if freqs.size else 0.0,
            "impedance_ohms_min": float(np.nanmin(z)) if z_has_finite else 0.0,
            "impedance_ohms_max": float(np.nanmax(z)) if z_has_finite else 0.0,
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
            instrument="impedance",
            sample_count=int(freqs.size),
            sample_rate_hz=None,
            extra=summary_extra,
        )
        res = self.artifacts.write_npz(
            instrument="impedance",
            arrays=arrays,
            config=config,
            summary=summary,
            output_path=Path(output_path) if output_path else None,
            description=description,
        )
        return {"path": res.path, "sidecar_path": res.sidecar_path, "summary": summary_extra}

    def release(self) -> None:
        self.device.allocator.release("impedance")
        self._config = None
