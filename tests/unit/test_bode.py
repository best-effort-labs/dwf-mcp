from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.bode_dsp import QF_NONCOHERENT, bode_point, detect_clip
from dwf_mcp.instruments.bode import Bode
from dwf_mcp.instruments.scope import Scope


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


# ---------------------------------------------------------------------------
# Task 5: Bode instrument (configure / measure)
# ---------------------------------------------------------------------------

def _dev(tmp_path):
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.policy import SafetyPolicy
    d = DwfDevice(backend=FakeBackend(), policy=SafetyPolicy(),
                  allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
                  workspace=tmp_path, idle_timeout_s=60)
    d.open()
    return d


def test_measure_sweeps_rc_lowpass(tmp_path):
    d = _dev(tmp_path)
    d.backend.set_bode_sim(ref_channel=1, dut_channel=2, fc_hz=1000.0, range_v=5.0)
    bode = Bode(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    bode.configure(start_hz=100.0, stop_hz=10_000.0, points=7, spacing="log",
                   amplitude_v=1.0, settle_min_s=0.0, settle_cycles=0)
    out = bode.measure()
    npz = np.load(out["path"])
    freqs = npz["frequency_hz"]
    gains = npz["gain_db"]
    phases = npz["phase_deg"]
    assert gains[0] == pytest.approx(0.0, abs=0.5)             # 100 Hz << fc
    i_corner = int(np.argmin(np.abs(freqs - 1000.0)))
    assert gains[i_corner] == pytest.approx(-3.0, abs=0.6)
    assert phases[i_corner] == pytest.approx(-45.0, abs=5.0)
    assert gains[-1] < -15.0                                   # 10 kHz >> fc
    assert out["summary"]["point_count"] == 7
    assert Path(out["sidecar_path"]).exists()


def test_measure_flags_noncoherent(tmp_path):
    d = _dev(tmp_path)
    d.backend.set_bode_sim(fc_hz=1000.0, rate_quantize=1.03)
    bode = Bode(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    bode.configure(start_hz=500.0, stop_hz=2000.0, points=3, settle_min_s=0.0, settle_cycles=0)
    out = bode.measure()
    qf = np.load(out["path"])["quality_flags"]
    assert int(qf[0]) & QF_NONCOHERENT


def test_measure_claims_then_releases(tmp_path):
    d = _dev(tmp_path)
    d.backend.set_bode_sim(fc_hz=1000.0)
    bode = Bode(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    bode.configure(start_hz=500.0, stop_hz=2000.0, points=3, settle_min_s=0.0, settle_cycles=0)
    bode.measure()
    assert d.allocator.claimed_pins() == {}  # awg + scope released


def test_measure_conflicts_with_live_scope(tmp_path):
    d = _dev(tmp_path)
    d.backend.set_bode_sim(fc_hz=1000.0)
    scope = Scope(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    scope.configure(channels=[2], range_v=5.0, sample_rate_hz=100_000.0, buffer_size=1024)
    bode = Bode(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    bode.configure(start_hz=500.0, stop_hz=2000.0, points=3, settle_min_s=0.0, settle_cycles=0)
    with pytest.raises(PinAllocationError):
        bode.measure()


def test_configure_rejects_same_ref_and_dut(tmp_path):
    d = _dev(tmp_path)
    bode = Bode(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    with pytest.raises(ValueError, match="ref_channel"):
        bode.configure(start_hz=100.0, stop_hz=1000.0, points=3, ref_channel=1, dut_channel=1)


def test_measure_warmup_absorbs_transient(tmp_path):
    d = _dev(tmp_path)
    d.backend.set_bode_sim(fc_hz=1000.0, transient_first=True)
    bode = Bode(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    bode.configure(start_hz=100.0, stop_hz=10_000.0, points=7, spacing="log",
                   amplitude_v=1.0, settle_min_s=0.0, settle_cycles=0)
    out = bode.measure()
    gains = np.load(out["path"])["gain_db"]
    assert gains[0] == pytest.approx(0.0, abs=0.5)   # not corrupted by the transient
