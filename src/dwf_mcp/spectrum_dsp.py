# src/dwf_mcp/spectrum_dsp.py
"""Pure DSP for the spectrum instrument — no hardware, no I/O. Unit-test target;
also the seed for the Network/Bode analyzer's single-bin extraction."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_WINDOWS = ("rectangular", "hann", "blackman", "flattop")
_AMPLITUDES = ("rms", "peak")
_FLATTOP = (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368)


@dataclass
class SpectrumResult:
    frequency_hz: np.ndarray
    magnitude_v: np.ndarray      # Vrms or Vpeak per `amplitude`
    magnitude_dbv: np.ndarray
    sample_rate_hz: float
    window: str
    amplitude: str
    rbw_hz: float
    enbw_hz: float


def _window(name: str, n: int) -> np.ndarray:
    if name == "rectangular":
        return np.ones(n)
    if name == "hann":
        return np.hanning(n)
    if name == "blackman":
        return np.blackman(n)
    if name == "flattop":
        k = np.arange(n)
        a = _FLATTOP
        return (a[0] - a[1] * np.cos(2 * np.pi * k / (n - 1))
                + a[2] * np.cos(4 * np.pi * k / (n - 1))
                - a[3] * np.cos(6 * np.pi * k / (n - 1))
                + a[4] * np.cos(8 * np.pi * k / (n - 1)))
    raise ValueError(f"window must be one of {_WINDOWS}, got {name!r}")


def compute_spectrum(
    samples: np.ndarray, sample_rate_hz: float,
    window: str = "hann", amplitude: str = "rms",
) -> SpectrumResult:
    if amplitude not in _AMPLITUDES:
        raise ValueError(f"amplitude must be one of {_AMPLITUDES}, got {amplitude!r}")
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n == 0:
        empty = np.array([])
        return SpectrumResult(empty, empty, empty, sample_rate_hz, window, amplitude, 0.0, 0.0)
    w = _window(window, n)
    cg = float(w.sum())                          # coherent gain
    freq = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    amp = np.abs(np.fft.rfft(x * w)) / cg        # peak amplitude per bin (corrected)
    # one-sided fold: x2 on AC bins, excluding DC (0) and (even-n) Nyquist (last)
    ac = slice(1, n // 2) if n % 2 == 0 else slice(1, None)
    amp[ac] *= 2.0
    if amplitude == "rms":
        amp[ac] /= np.sqrt(2.0)
    rbw_hz = sample_rate_hz / n
    enbw_hz = sample_rate_hz * float(np.sum(w ** 2)) / (cg ** 2)
    with np.errstate(divide="ignore"):
        mag_dbv = 20.0 * np.log10(np.maximum(amp, 1e-15))
    return SpectrumResult(freq, amp, mag_dbv, sample_rate_hz, window, amplitude, rbw_hz, enbw_hz)
