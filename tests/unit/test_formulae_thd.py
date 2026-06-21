# tests/unit/test_formulae_thd.py
from __future__ import annotations

import numpy as np

from dwf_mcp import formulae
from dwf_mcp.spectrum_dsp import compute_spectrum


def _multi_harmonic(sr: float, n: int, f0: float, amps: dict[int, float]) -> np.ndarray:
    """amps: dict harmonic_number -> peak amplitude (1 = fundamental)."""
    t = np.arange(n) / sr
    sig = np.zeros(n)
    for k, a in amps.items():
        sig += a * np.sin(2 * np.pi * k * f0 * t)
    return sig


def test_thd_recovers_known_distortion() -> None:
    sr, n = 200_000.0, 8192
    f0 = 40 * sr / n  # coherent: integer bins
    amps = {1: 1.0, 2: 0.10, 3: 0.05}  # THD = sqrt(.1^2 + .05^2)/1
    sig = _multi_harmonic(sr, n, f0, amps)
    result = compute_spectrum(sig, sr, window="rectangular", amplitude="rms")
    thd = formulae.thd(result, fundamental_hz=f0, n_harmonics=5)
    expected = np.sqrt(0.10**2 + 0.05**2)
    assert abs(thd - expected) < 0.005


def test_snr_high_for_clean_tone() -> None:
    sr, n = 200_000.0, 8192
    f0 = 40 * sr / n
    sig = _multi_harmonic(sr, n, f0, {1: 1.0})
    result = compute_spectrum(sig, sr, window="rectangular", amplitude="rms")
    snr_db = formulae.snr_db(result, fundamental_hz=f0)
    assert snr_db > 60.0
