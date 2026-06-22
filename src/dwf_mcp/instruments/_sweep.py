"""Shared sweep orchestration for the frequency-domain analyzers (bode, impedance).

Both instruments drive an AWG sine across a frequency grid and capture two scope
channels ratiometrically; the orchestration and per-point acquisition are identical
— they diverge only in the per-point DSP/quality mapping and the artifact writer.
That shared machinery lives here, in one place, so the load-bearing invariants are
stated once:

  * **Actuals come from hardware readbacks ONLY** — never fall back to the
    requested/planned freq or rate. A soft-failed readback (0/garbage) would make
    the capture look coherent-by-construction and silently suppress the noncoherent
    flag.
  * **Settle BEFORE arm.** ``scope_arm()`` starts the single acquisition, so settling
    after the arm would capture the post-retune transient.
  * **Gate once per sweep** (amplitude is constant across all points) and clear any
    stale trigger before the warm-up.

Each instrument supplies a ``record_point`` callback that maps a :class:`SweepPoint`
to its own columns (computing DSP fields and OR-ing in its own guard flags), and
keeps its own ``_write``.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from dwf_mcp.device import DwfDevice
from dwf_mcp.sweep_dsp import assess_quality, frequency_grid, plan_acquisition


@dataclass
class SweepPoint:
    """One acquired sweep point. ``qflags`` holds the base flags (acquisition clamp
    + coherence); the per-instrument ``record_point`` computes DSP fields from
    ``vin``/``vout`` and ORs in its own guard flags."""

    f_act: float
    sr_act: float
    n: int
    achieved_cycles: float
    spc: float
    coh_err: float
    qflags: int
    vin: np.ndarray  # ref-channel samples
    vout: np.ndarray  # dut-channel samples


def _warm_up(be: Any, ref: int, dut: int, n_ch: int) -> None:
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


def _settle(freq_hz: float, cfg: dict[str, Any]) -> None:
    if cfg["settle_s"] is not None:
        base = cfg["settle_s"]
    else:
        base = cfg["settle_cycles"] / freq_hz if freq_hz else 0.0
    delay = max(base, cfg["settle_min_s"])
    if delay > 0:
        time.sleep(delay)


def _await_done(be: Any, plan: Any, instrument_name: str) -> None:
    deadline = time.monotonic() + max(
        plan.buffer_size / plan.sample_rate_hz * 10 + 1.0, 2.0
    )
    while time.monotonic() < deadline:
        if be.scope_status() == "Done":
            return
        time.sleep(0.002)
    raise RuntimeError(f"{instrument_name} capture did not complete before deadline")


def _acquire_point(
    be: Any,
    info: Any,
    f_req: float,
    cfg: dict[str, Any],
    ref: int,
    dut: int,
    n_ch: int,
    instrument_name: str,
) -> SweepPoint:
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
    _settle(f_act, cfg)
    be.scope_arm()
    _await_done(be, plan, instrument_name)
    vin = np.asarray(be.scope_read(channel=ref, count=plan.buffer_size))
    vout = np.asarray(be.scope_read(channel=dut, count=plan.buffer_size))
    n = plan.buffer_size
    achieved_cycles = f_act * n / sr_act
    spc = sr_act / f_act
    qflags, coh_err = assess_quality(
        achieved_cycles, spc, f_act, sr_act, cfg["min_cycles"]
    )
    qflags |= plan.clamp_flags
    return SweepPoint(
        f_act=f_act,
        sr_act=sr_act,
        n=n,
        achieved_cycles=achieved_cycles,
        spc=spc,
        coh_err=coh_err,
        qflags=qflags,
        vin=vin,
        vout=vout,
    )


def run_sweep(
    device: DwfDevice,
    info: Any,
    *,
    instrument_name: str,
    cfg: dict[str, Any],
    column_keys: tuple[str, ...],
    record_point: Callable[[SweepPoint, dict[str, list[float]]], None],
) -> tuple[dict[str, list[float]], list[float], list[int]]:
    """Run the full sweep: claim → gate-once → clear stale trigger → warm-up →
    per-point acquisition loop → (finally) stop AWG → release the claim.

    ``record_point(point, cols)`` appends the instrument-specific columns for one
    acquired point. Returns the accumulated columns plus the per-point actual
    sample-rate and buffer-size lists (for the sidecar)."""
    be = device.backend
    drive, ref, dut = cfg["drive_channel"], cfg["ref_channel"], cfg["dut_channel"]
    n_ch = info.analog_in_channels
    pins = [f"awg{drive}"] + [f"scope{i}" for i in range(1, n_ch + 1)]
    freqs = frequency_grid(cfg["start_hz"], cfg["stop_hz"], cfg["points"], cfg["spacing"])
    cols: dict[str, list[float]] = {k: [] for k in column_keys}
    sr_seen: list[float] = []
    buf_seen: list[int] = []
    device.allocator.claim(instrument_name, pins)
    try:
        # Gate once for the whole sweep: amplitude is constant across all points, so
        # one authorization covers every per-point awg_start (which call the backend
        # directly to avoid per-frequency gate/log overhead during a long sweep).
        device.gate_output("awg_start", channel=drive, amplitude=cfg["amplitude_v"])
        # Clear any trigger left configured by a prior scope use: the sweep free-runs
        # each capture (settle, then arm), so a stale edge trigger would stall
        # acquisition until the auto-timeout or capture at a trigger-dependent phase.
        be.scope_set_trigger(source="none", channel=None, level_v=0.0,
                             condition="Either", position_s=0.0, timeout_s=0.0)
        _warm_up(be, ref, dut, n_ch)
        for f_req in freqs:
            point = _acquire_point(be, info, float(f_req), cfg, ref, dut, n_ch,
                                   instrument_name)
            record_point(point, cols)
            sr_seen.append(point.sr_act)
            buf_seen.append(point.n)
    finally:
        try:
            be.awg_stop(channel=drive)
        finally:
            device.allocator.release(instrument_name)
    return cols, sr_seen, buf_seen
