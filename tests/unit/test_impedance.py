from __future__ import annotations

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
