"""Pure DSP for the spectrum instrument — no hardware, no I/O. Unit-test target;
also the seed for the Network/Bode analyzer's single-bin extraction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


def summarize_spectrum(result: SpectrumResult) -> dict[str, Any]:
    freq, mag_v, mag_dbv = result.frequency_hz, result.magnitude_v, result.magnitude_dbv
    # < 2 bins means no AC content to pick a peak from (empty input, or a 1-sample
    # capture passed to transform()): report DC-only rather than argmax-ing an empty slice.
    if mag_v.size < 2:
        dc_only = float(mag_dbv[0]) if mag_v.size else 0.0
        return {
            "peak_frequency_hz": 0.0, "peak_magnitude_dbv": dc_only,
            "dc_magnitude_dbv": dc_only, "noise_floor_dbv": 0.0,
            "rbw_hz": float(result.rbw_hz), "enbw_hz": float(result.enbw_hz),
            "span_hz": float(freq[-1]) if mag_v.size else 0.0,
            "window": result.window, "amplitude": result.amplitude,
        }
    dc_dbv = float(mag_dbv[0])
    ac = mag_v[1:]                              # exclude DC for the peak search
    k = int(np.argmax(ac)) + 1                  # index into the full array
    # 3-bin quadratic interpolation over log-magnitude (dB): accurate for the peak
    # FREQUENCY; the amplitude estimate is window-dependent (near-exact for flattop,
    # ~0.3 dB worst-case for hann at a half-bin offset). Guard the array edges.
    if 1 <= k <= mag_dbv.size - 2:
        ym1, y0, yp1 = mag_dbv[k - 1], mag_dbv[k], mag_dbv[k + 1]
        denom = (ym1 - 2 * y0 + yp1)
        p = 0.5 * (ym1 - yp1) / denom if denom != 0 else 0.0
        peak_dbv = float(y0 - 0.25 * (ym1 - yp1) * p)
        peak_freq = float((k + p) * result.rbw_hz)
    else:
        peak_dbv, peak_freq = float(mag_dbv[k]), float(freq[k])
    # noise floor: median of non-DC bins excluding a +/-3-bin window around the peak
    mask = np.ones(mag_dbv.size, dtype=bool)
    mask[0] = False
    lo, hi = max(1, k - 3), min(mag_dbv.size, k + 4)
    mask[lo:hi] = False
    noise = float(np.median(mag_dbv[mask])) if mask.any() else float(mag_dbv[1:].min())
    return {
        "peak_frequency_hz": peak_freq,
        "peak_magnitude_dbv": peak_dbv,
        "dc_magnitude_dbv": dc_dbv,
        "noise_floor_dbv": noise,
        "rbw_hz": float(result.rbw_hz),
        "enbw_hz": float(result.enbw_hz),
        "span_hz": float(freq[-1]),
        "window": result.window,
        "amplitude": result.amplitude,
    }
