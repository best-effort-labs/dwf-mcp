from __future__ import annotations

from dwf_mcp.backends.fake import FakeBackend


def test_fake_awg_frequency_get_returns_requested():
    be = FakeBackend()
    be.awg_configure(channel=1, function="Sine", freq_hz=12_345.0, amplitude_v=1.0,
                     offset_v=0.0, phase_deg=0.0, symmetry=50.0, run_time_s=None)
    assert be.awg_frequency_get(1) == 12_345.0


def test_fake_scope_sample_rate_get_returns_requested():
    be = FakeBackend()
    be.scope_set_acquisition(sample_rate_hz=64_000.0, buffer_size=1024, mode="Single")
    assert be.scope_sample_rate_get() == 64_000.0
