from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.impedance_dsp import impedance_point
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.impedance import Impedance
from dwf_mcp.policy import SafetyPolicy
from dwf_mcp.sweep_dsp import QF_LOW_DRIVE, QF_LOW_DUT_VOLTAGE


def _read_two(be: FakeBackend, ref: int, dut: int, n: int):
    be.scope_arm()
    return (np.asarray(be.scope_read(channel=ref, count=n)),
            np.asarray(be.scope_read(channel=dut, count=n)))


def test_impedance_sim_forward_divider_recovers_resistor():
    be = FakeBackend()
    sr, n, r_ref, freq = 1_000_000.0, 8192, 1000.0, 32 * 1_000_000.0 / 8192
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=r_ref, model="R", r=470.0)
    be.awg_configure(channel=1, function="Sine", freq_hz=freq, amplitude_v=1.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    be.awg_start(channel=1)
    be.scope_set_acquisition(sample_rate_hz=sr, buffer_size=n, mode="Single")
    v_total, v_dut = _read_two(be, 1, 2, n)
    out = impedance_point(v_total, v_dut, be.scope_sample_rate_get(),
                          be.awg_frequency_get(1), r_ref)
    assert out["impedance_ohms"] == pytest.approx(470.0, rel=2e-3)
    assert out["phase_deg"] == pytest.approx(0.0, abs=0.3)


def test_impedance_sim_capacitor_minus90():
    be = FakeBackend()
    sr, n, r_ref, c, freq = 1_000_000.0, 8192, 1000.0, 100e-9, 32 * 1_000_000.0 / 8192
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=r_ref, model="C", c=c)
    be.awg_configure(channel=1, function="Sine", freq_hz=freq, amplitude_v=1.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    be.awg_start(channel=1)
    be.scope_set_acquisition(sample_rate_hz=sr, buffer_size=n, mode="Single")
    v_total, v_dut = _read_two(be, 1, 2, n)
    out = impedance_point(v_total, v_dut, be.scope_sample_rate_get(),
                          be.awg_frequency_get(1), r_ref)
    assert out["phase_deg"] == pytest.approx(-90.0, abs=1.0)
    assert out["capacitance_f"] == pytest.approx(c, rel=1e-2)


def test_impedance_sim_quantization_is_applied_to_getters():
    be = FakeBackend()
    be.set_impedance_sim(r_ref=1000.0, model="R", r=1000.0,
                         freq_quantize=1.001, rate_quantize=0.999)
    be.awg_configure(channel=1, function="Sine", freq_hz=10_000.0, amplitude_v=1.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    be.awg_start(channel=1)
    be.scope_set_acquisition(sample_rate_hz=500_000.0, buffer_size=4096, mode="Single")
    assert be.awg_frequency_get(1) == pytest.approx(10_010.0, rel=1e-9)
    assert be.scope_sample_rate_get() == pytest.approx(499_500.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Task 4: Impedance instrument (configure / skeleton)
# ---------------------------------------------------------------------------

def _make_device(tmp_path: Path, be: FakeBackend | None = None) -> DwfDevice:
    dev = DwfDevice(backend=be or FakeBackend(), policy=SafetyPolicy(),
                    allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
                    workspace=tmp_path, idle_timeout_s=60)
    dev.open()
    return dev


def _impedance(tmp_path: Path, be: FakeBackend | None = None) -> Impedance:
    dev = _make_device(tmp_path, be)
    return Impedance(device=dev, artifacts=ArtifactWriter(workspace=dev.workspace))


def test_configure_requires_r_ref_positive(tmp_path):
    imp = _impedance(tmp_path)
    with pytest.raises(ValueError, match="r_ref"):
        imp.configure(start_hz=100.0, stop_hz=100_000.0, points=10, r_ref=0.0)


def test_configure_rejects_equal_channels(tmp_path):
    imp = _impedance(tmp_path)
    with pytest.raises(ValueError, match="differ"):
        imp.configure(start_hz=100.0, stop_hz=100_000.0, points=10, r_ref=1000.0,
                      ref_channel=2, dut_channel=2)


def test_configure_rejects_bad_sweep_range(tmp_path):
    imp = _impedance(tmp_path)
    with pytest.raises(ValueError, match="stop_hz"):
        imp.configure(start_hz=1000.0, stop_hz=100.0, points=10, r_ref=1000.0)


def test_configure_returns_echo(tmp_path):
    imp = _impedance(tmp_path)
    out = imp.configure(start_hz=100.0, stop_hz=100_000.0, points=12, r_ref=1000.0)
    assert out["configured"] is True
    assert out["r_ref"] == 1000.0 and out["points"] == 12


def test_measure_before_configure_raises(tmp_path):
    imp = _impedance(tmp_path)
    with pytest.raises(InstrumentNotConfigured):
        imp.measure()


# ---------------------------------------------------------------------------
# Task 5: Impedance.measure() sweep orchestration + quality guards
# ---------------------------------------------------------------------------

def _sweep(be: FakeBackend, tmp_path, **cfg):
    imp = _impedance(tmp_path, be)
    imp.configure(**cfg)
    out = imp.measure()
    return out, np.load(out["path"])


def test_measure_resistor_flat_impedance(tmp_path):
    be = FakeBackend()
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=1000.0, model="R", r=1000.0)
    out, npz = _sweep(be, tmp_path, start_hz=100.0, stop_hz=100_000.0, points=12,
                      r_ref=1000.0, amplitude_v=1.0)
    z = npz["impedance_ohms"]
    ph = npz["phase_deg"]
    assert np.max(np.abs(z - 1000.0)) < 5.0
    assert np.max(np.abs(ph)) < 1.0
    assert out["summary"]["point_count"] == 12


def test_measure_capacitor_phase_and_C(tmp_path):
    be = FakeBackend()
    c = 100e-9
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=1000.0, model="C", c=c)
    out, npz = _sweep(be, tmp_path, start_hz=500.0, stop_hz=5000.0, points=12,
                      r_ref=1000.0, amplitude_v=1.0)
    assert np.median(npz["phase_deg"]) == pytest.approx(-90.0, abs=3.0)
    assert np.nanmedian(npz["capacitance_f"]) == pytest.approx(c, rel=0.05)


def test_measure_low_dut_voltage_flag_when_Z_far_below_rref(tmp_path):
    be = FakeBackend()
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=1_000_000.0, model="R", r=10.0)
    out, npz = _sweep(be, tmp_path, start_hz=1000.0, stop_hz=10_000.0, points=6,
                      r_ref=1_000_000.0, amplitude_v=1.0, min_dut_rms=0.05)
    assert np.all((npz["quality_flags"] & QF_LOW_DUT_VOLTAGE) != 0)


def test_measure_low_drive_flag_when_Z_far_above_rref(tmp_path):
    be = FakeBackend()
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=1.0, model="R", r=1_000_000.0)
    out, npz = _sweep(be, tmp_path, start_hz=1000.0, stop_hz=10_000.0, points=6,
                      r_ref=1.0, amplitude_v=1.0, min_drive_rms=0.05)
    assert np.all((npz["quality_flags"] & QF_LOW_DRIVE) != 0)


def test_measure_releases_claim_and_writes_sidecar(tmp_path):
    be = FakeBackend()
    be.set_impedance_sim(r_ref=1000.0, model="R", r=1000.0)
    imp = _impedance(tmp_path, be)
    imp.configure(start_hz=100.0, stop_hz=10_000.0, points=4, r_ref=1000.0)
    out = imp.measure()
    assert "impedance" not in imp.device.allocator.claimed_instruments()
    assert Path(out["sidecar_path"]).exists()


def test_measure_all_floored_sweep_summary_is_json_clean(tmp_path):
    # Z >> R_ref everywhere makes V_total ~ V_dut -> drive floored -> |Z| all NaN.
    # The summary must still be finite + JSON-serializable (no NaN leaking into the sidecar).
    be = FakeBackend()
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=1e-6, model="R", r=1e9)
    out, npz = _sweep(be, tmp_path, start_hz=1000.0, stop_hz=10_000.0, points=4,
                      r_ref=1e-6, amplitude_v=1.0)
    summ = out["summary"]
    assert np.isfinite(summ["impedance_ohms_min"])
    assert np.isfinite(summ["impedance_ohms_max"])
    # strict JSON (allow_nan=False) must succeed
    json.dumps(summ, allow_nan=False)


def test_set_impedance_sim_clears_bode_sim(tmp_path):
    # Switching a reused FakeBackend from bode sim to impedance sim must take effect
    # (bode sim was checked first in scope_read, so a stale bode sim would mask it).
    be = FakeBackend()
    be.set_bode_sim(ref_channel=1, dut_channel=2, fc_hz=1000.0)
    be.set_impedance_sim(ref_channel=1, dut_channel=2, r_ref=1000.0, model="R", r=470.0)
    assert be._bode_sim is None
    sr, n, freq = 1_000_000.0, 8192, 32 * 1_000_000.0 / 8192
    be.awg_configure(channel=1, function="Sine", freq_hz=freq, amplitude_v=1.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    be.awg_start(channel=1)
    be.scope_set_acquisition(sample_rate_hz=sr, buffer_size=n, mode="Single")
    be.scope_arm()
    v_total = np.asarray(be.scope_read(channel=1, count=n))
    v_dut = np.asarray(be.scope_read(channel=2, count=n))
    out = impedance_point(v_total, v_dut, be.scope_sample_rate_get(),
                          be.awg_frequency_get(1), 1000.0)
    assert out["impedance_ohms"] == pytest.approx(470.0, rel=2e-3)
