from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.bode_dsp import (
    QF_BUFFER_LIMITED,
    QF_LOW_CYCLES,
    QF_LOW_OVERSAMPLING,
    QF_LOW_SAMPLES_PER_CYCLE,
    QF_NEAR_NYQUIST,
    QF_NONCOHERENT,
    QF_SAMPLE_RATE_LIMITED,
    QF_VERY_LOW_SAMPLES_PER_CYCLE,
    assess_quality,
    bode_point,
    detect_clip,
    extract_tone,
    frequency_grid,
    plan_acquisition,
)


def _cos(freq, amp, sr, n, phase_deg=0.0):
    t = np.arange(n) / sr
    return amp * np.cos(2 * np.pi * freq * t + np.deg2rad(phase_deg))


def test_extract_tone_amplitude_and_phase_coherent():
    sr, n = 1_000_000.0, 4096
    freq = 16 * sr / n  # exactly 16 cycles in the record -> coherent
    x = _cos(freq, 2.0, sr, n, phase_deg=30.0)
    ph = extract_tone(x, sr, freq)
    assert abs(ph) == pytest.approx(2.0 / np.sqrt(2.0), rel=1e-3)        # Vrms of a 2 V peak
    assert np.rad2deg(np.angle(ph)) == pytest.approx(30.0, abs=0.05)


def test_extract_tone_near_coherent_offbin():
    # Non-integer cycles (hardware-quantization analogue): still close over many cycles.
    sr, n = 1_000_000.0, 4096
    freq = 16.3 * sr / n
    x = _cos(freq, 1.0, sr, n, phase_deg=-45.0)
    ph = extract_tone(x, sr, freq)
    assert abs(ph) == pytest.approx(1.0 / np.sqrt(2.0), rel=0.05)
    assert np.rad2deg(np.angle(ph)) == pytest.approx(-45.0, abs=3.0)


def test_extract_tone_empty():
    assert extract_tone(np.array([]), 1_000_000.0, 1000.0) == 0j


def test_bode_point_known_gain_and_phase():
    sr, n = 1_000_000.0, 4096
    freq = 16 * sr / n
    vin = _cos(freq, 1.0, sr, n, phase_deg=0.0)
    vout = _cos(freq, 0.5, sr, n, phase_deg=-90.0)  # half amplitude, 90deg lag
    p = bode_point(vin, vout, sr, freq)
    assert p["gain_db"] == pytest.approx(-6.0206, abs=0.02)   # 20log10(0.5)
    assert p["phase_deg"] == pytest.approx(-90.0, abs=0.1)
    assert p["vin_rms"] == pytest.approx(1.0 / np.sqrt(2.0), rel=1e-3)
    assert p["vout_rms"] == pytest.approx(0.5 / np.sqrt(2.0), rel=1e-3)


def test_bode_point_phase_wraps_to_180():
    sr, n = 1_000_000.0, 4096
    freq = 16 * sr / n
    vin = _cos(freq, 1.0, sr, n, phase_deg=0.0)
    vout = _cos(freq, 1.0, sr, n, phase_deg=180.0)  # inverted
    p = bode_point(vin, vout, sr, freq)
    assert abs(p["phase_deg"]) == pytest.approx(180.0, abs=0.1)  # +/-180
    assert p["gain_db"] == pytest.approx(0.0, abs=0.02)


def test_bode_point_zero_vin_does_not_crash():
    sr, n = 1_000_000.0, 4096
    freq = 16 * sr / n
    vin = np.zeros(n)
    vout = _cos(freq, 1.0, sr, n)
    p = bode_point(vin, vout, sr, freq)
    assert p["vin_rms"] == pytest.approx(0.0, abs=1e-9)
    assert np.isfinite(p["gain_db"])  # floored, not inf/nan
    assert p["gain_db"] < -200.0      # the dead-vin floor sentinel, not a real ratio


# ---------------------------------------------------------------------------
# Task 2 — planner, quality flags, frequency grid, clip detection
# ---------------------------------------------------------------------------


def test_plan_acquisition_coherent_unclamped():
    # 1 kHz, plenty of headroom -> integer cycles, no flags.
    p = plan_acquisition(1000.0, max_sample_rate_hz=100e6, max_buffer=1 << 20,
                         samples_per_cycle=64, min_cycles=16)
    assert p.sample_rate_hz == pytest.approx(64_000.0)
    assert p.buffer_size == 16 * 64                       # 1024 = 16 cycles * 64 spc
    assert p.cycles == pytest.approx(16.0)
    assert p.clamp_flags == 0


def test_plan_acquisition_high_freq_rate_clamped():
    # Demand 64 spc at 5 MHz = 320 MHz > 100 MHz cap -> sample_rate_limited.
    p = plan_acquisition(5e6, max_sample_rate_hz=100e6, max_buffer=1 << 20,
                         samples_per_cycle=64, min_cycles=16)
    assert p.sample_rate_hz == pytest.approx(100e6)
    assert p.clamp_flags & QF_SAMPLE_RATE_LIMITED


def test_plan_acquisition_low_freq_buffer_clamped():
    # 1 Hz, 64 spc -> unclamped buffer = 16*64 = 1024 > 512 cap
    # -> buffer_limited flag + cycles forced below min_cycles.
    p = plan_acquisition(1.0, max_sample_rate_hz=100e6, max_buffer=512,
                         samples_per_cycle=64, min_cycles=16)
    assert p.buffer_size == 512
    assert p.clamp_flags & QF_BUFFER_LIMITED
    assert p.cycles < 16.0


def test_assess_quality_clean_point():
    flags, coh_err = assess_quality(achieved_cycles=16.0, samples_per_cycle=64.0,
                                    freq_hz=1000.0, sample_rate_hz=64_000.0,
                                    min_cycles=16)
    assert flags == 0
    assert coh_err == pytest.approx(0.0, abs=1e-9)


def test_assess_quality_noncoherent_flag():
    flags, coh_err = assess_quality(achieved_cycles=16.3, samples_per_cycle=64.0,
                                    freq_hz=1000.0, sample_rate_hz=64_000.0,
                                    min_cycles=16)
    assert flags & QF_NONCOHERENT
    assert coh_err == pytest.approx(0.3, abs=1e-9)


def test_assess_quality_low_and_very_low_samples_per_cycle():
    f_low, _ = assess_quality(16.0, 15.0, 1000.0, 15_000.0, 16)   # <20
    assert f_low & QF_LOW_SAMPLES_PER_CYCLE
    assert not (f_low & QF_VERY_LOW_SAMPLES_PER_CYCLE)
    f_vlow, _ = assess_quality(16.0, 8.0, 1000.0, 8000.0, 16)     # <10
    assert f_vlow & QF_VERY_LOW_SAMPLES_PER_CYCLE


def test_assess_quality_oversampling_and_nyquist():
    # freq = fs/8 -> > fs/10 (low_oversampling) but < 0.4*fs (not near_nyquist)
    f1, _ = assess_quality(16.0, 8.0, 1000.0, 8000.0, 16)
    assert f1 & QF_LOW_OVERSAMPLING
    assert not (f1 & QF_NEAR_NYQUIST)
    # freq = 0.45*fs -> near_nyquist
    f2, _ = assess_quality(16.0, 2.2, 4500.0, 10_000.0, 16)
    assert f2 & QF_NEAR_NYQUIST


def test_assess_quality_low_cycles():
    flags, _ = assess_quality(4.0, 64.0, 1000.0, 64_000.0, 16)
    assert flags & QF_LOW_CYCLES


def test_frequency_grid_log_and_linear():
    g_log = frequency_grid(10.0, 100_000.0, 5, "log")
    assert g_log[0] == pytest.approx(10.0) and g_log[-1] == pytest.approx(100_000.0)
    assert g_log[2] == pytest.approx(1000.0, rel=1e-6)  # geometric midpoint
    g_lin = frequency_grid(0.0, 100.0, 3, "linear")
    assert list(g_lin) == pytest.approx([0.0, 50.0, 100.0])
    with pytest.raises(ValueError):
        frequency_grid(10.0, 100.0, 1, "log")
    with pytest.raises(ValueError):
        frequency_grid(10.0, 100.0, 5, "bogus")


def test_detect_clip():
    assert detect_clip(np.array([0.1, -0.99, 0.3]), range_v=1.0) is True
    assert detect_clip(np.array([0.1, -0.5, 0.3]), range_v=1.0) is False
    assert detect_clip(np.array([]), range_v=1.0) is False


def test_plan_acquisition_rejects_nonpositive_freq():
    with pytest.raises(ValueError, match="freq_hz"):
        plan_acquisition(0.0, max_sample_rate_hz=100e6, max_buffer=1 << 20)
    with pytest.raises(ValueError, match="freq_hz"):
        plan_acquisition(-5.0, max_sample_rate_hz=100e6, max_buffer=1 << 20)


def test_plan_acquisition_buffer_floor_keeps_cycles_consistent():
    # Degenerate cap below the 16-sample floor: buffer is bumped to 16, and cycles must
    # stay consistent with the FINAL buffer (buffer_size == cycles * samples_per_cycle).
    p = plan_acquisition(1000.0, max_sample_rate_hz=100e6, max_buffer=8,
                         samples_per_cycle=64, min_cycles=16)
    assert p.buffer_size == 16
    assert p.clamp_flags & QF_BUFFER_LIMITED
    assert p.cycles == pytest.approx(16 / 64)                       # 0.25
    assert p.buffer_size == pytest.approx(p.cycles * p.samples_per_cycle)
