# tests/unit/test_spectrum_dsp.py
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.spectrum_dsp import compute_spectrum, summarize_spectrum


def _sine(freq, amp, sr, n):
    t = np.arange(n) / sr
    return amp * np.sin(2 * np.pi * freq * t)


def test_bin_centered_amplitude_rms_and_peak():
    sr, n, amp = 100_000.0, 4096, 1.0
    # choose a bin-centered frequency: k * sr/n
    freq = 50 * sr / n  # exactly on bin 50
    x = _sine(freq, amp, sr, n)
    r_peak = compute_spectrum(x, sr, window="rectangular", amplitude="peak")
    r_rms = compute_spectrum(x, sr, window="rectangular", amplitude="rms")
    k = int(round(freq / (sr / n)))
    assert r_peak.magnitude_v[k] == pytest.approx(1.0, abs=1e-3)        # peak amplitude
    assert r_rms.magnitude_v[k] == pytest.approx(1.0 / np.sqrt(2), abs=1e-3)  # Vrms
    assert r_peak.frequency_hz[k] == pytest.approx(freq, abs=1e-6)


def test_dc_not_doubled_and_separate():
    sr, n = 100_000.0, 4096
    x = np.full(n, 2.0)  # pure DC = 2 V
    r = compute_spectrum(x, sr, window="rectangular", amplitude="peak")
    assert r.magnitude_v[0] == pytest.approx(2.0, abs=1e-3)  # DC bin = 2 V, not 4


def test_window_coherent_gain_hann_amplitude():
    sr, n, amp = 100_000.0, 4096, 0.5
    freq = 100 * sr / n
    x = _sine(freq, amp, sr, n)
    r = compute_spectrum(x, sr, window="hann", amplitude="peak")
    k = int(round(freq / (sr / n)))
    assert r.magnitude_v[k] == pytest.approx(0.5, rel=0.02)  # coherent-gain corrected


def test_empty_input():
    r = compute_spectrum(np.array([]), 100_000.0, window="hann", amplitude="rms")
    assert r.frequency_hz.size == 0 and r.magnitude_v.size == 0


def test_flattop_is_available():
    r = compute_spectrum(_sine(1000.0, 1.0, 100_000.0, 1024), 100_000.0,
                         window="flattop", amplitude="rms")
    assert r.frequency_hz.size == 513


def test_summary_offbin_frequency_interpolated():
    # Frequency interpolation is the reliable off-bin guarantee (window-independent-ish).
    sr, n = 100_000.0, 4096
    freq = 50.5 * sr / n  # deliberately BETWEEN bins 50 and 51
    s = summarize_spectrum(compute_spectrum(_sine(freq, 1.0, sr, n),
                                            sr, window="hann", amplitude="peak"))
    assert s["peak_frequency_hz"] == pytest.approx(freq, rel=0.001)  # not snapped to a bin
    assert s["amplitude"] == "peak"


def test_summary_offbin_amplitude_flattop():
    # Amplitude is trustworthy off-bin with flattop (~0.02 dB scalloping). The dB-parabola
    # amplitude estimate is window-dependent, so the tight amplitude check uses flattop;
    # for hann a half-bin tone can be ~0.3 dB off, which is why we DON'T assert it there.
    sr, n = 100_000.0, 4096
    freq = 50.5 * sr / n
    s = summarize_spectrum(compute_spectrum(_sine(freq, 1.0, sr, n),
                                            sr, window="flattop", amplitude="peak"))
    assert s["peak_magnitude_dbv"] == pytest.approx(0.0, abs=0.2)  # 1.0 V peak -> ~0 dBV
    assert s["peak_frequency_hz"] == pytest.approx(freq, rel=0.005)


def test_summary_rms_vs_peak_differ_by_sqrt2():
    sr, n = 100_000.0, 4096
    freq = 50 * sr / n
    x = _sine(freq, 1.0, sr, n)
    sp = summarize_spectrum(compute_spectrum(x, sr, window="rectangular", amplitude="peak"))
    sr_ = summarize_spectrum(compute_spectrum(x, sr, window="rectangular", amplitude="rms"))
    assert sp["peak_magnitude_dbv"] - sr_["peak_magnitude_dbv"] == pytest.approx(3.0103, abs=0.05)


def test_summary_excludes_dc_from_peak():
    sr, n = 100_000.0, 4096
    x = 5.0 + _sine(50 * sr / n, 0.2, sr, n)  # big DC + small tone
    s = summarize_spectrum(compute_spectrum(x, sr, window="rectangular", amplitude="peak"))
    assert s["peak_frequency_hz"] == pytest.approx(50 * sr / n, rel=0.01)  # the tone, not DC
    assert s["dc_magnitude_dbv"] > s["peak_magnitude_dbv"]                  # DC reported separately
    assert s["enbw_hz"] > 0 and s["rbw_hz"] > 0


def test_summary_empty():
    s = summarize_spectrum(compute_spectrum(np.array([]), 100_000.0))
    assert s["peak_frequency_hz"] == 0.0 and s["peak_magnitude_dbv"] == 0.0
