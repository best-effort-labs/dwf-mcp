# tests/unit/test_spectrum_dsp.py
from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.spectrum_dsp import compute_spectrum


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
