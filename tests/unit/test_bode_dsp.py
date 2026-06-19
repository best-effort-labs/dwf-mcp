from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.bode_dsp import bode_point, extract_tone


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
