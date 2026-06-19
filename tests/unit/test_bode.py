from __future__ import annotations

import pytest

from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.bode_dsp import bode_point, detect_clip


def test_fake_awg_frequency_get_returns_requested():
    be = FakeBackend()
    be.awg_configure(channel=1, function="Sine", freq_hz=12_345.0, amplitude_v=1.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    assert be.awg_frequency_get(1) == 12_345.0


def test_fake_scope_sample_rate_get_returns_requested():
    be = FakeBackend()
    be.scope_set_acquisition(sample_rate_hz=64_000.0, buffer_size=1024, mode="Single")
    assert be.scope_sample_rate_get() == 64_000.0


def _setup_sim(be: FakeBackend, ref_ch: int = 1, dut_ch: int = 2, **kw: object) -> None:
    # One-pole RC low-pass with corner fc; H(f) = 1/(1 + j f/fc).
    be.set_bode_sim(ref_channel=ref_ch, dut_channel=dut_ch, fc_hz=1000.0, range_v=5.0, **kw)


def test_bode_sim_lowpass_gain_phase_at_corner():
    be = FakeBackend()
    _setup_sim(be)
    sr, n, f = 64_000.0, 1024, 1000.0  # at the corner fc
    be.awg_configure(channel=1, function="Sine", freq_hz=f, amplitude_v=1.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    be.scope_set_acquisition(sample_rate_hz=sr, buffer_size=n, mode="Single")
    vin = be.scope_read(channel=1, count=n)
    vout = be.scope_read(channel=2, count=n)
    p = bode_point(vin, vout, sr, f)
    assert p["gain_db"] == pytest.approx(-3.0103, abs=0.2)   # -3 dB at corner
    assert p["phase_deg"] == pytest.approx(-45.0, abs=2.0)   # -45 deg at corner


def test_bode_sim_clipping_detectable():
    be = FakeBackend()
    _setup_sim(be, clip=True)  # amplitude exceeds range -> clipped samples
    be.awg_configure(channel=1, function="Sine", freq_hz=1000.0, amplitude_v=10.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    be.scope_set_acquisition(sample_rate_hz=64_000.0, buffer_size=1024, mode="Single")
    vin = be.scope_read(channel=1, count=1024)
    assert detect_clip(vin, range_v=5.0) is True
