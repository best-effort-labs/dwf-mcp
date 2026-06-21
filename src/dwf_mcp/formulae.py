"""Pure derived-measurement formulae over freq-domain artifacts. No device/MCP — these
are the cookbook's validated reference implementations (promote-to-tool is deferred)."""
from __future__ import annotations

import numpy as np

from dwf_mcp.spectrum_dsp import SpectrumResult


def _bin_amp(result: SpectrumResult, freq_hz: float) -> float:
    """RMS amplitude at the nearest bin to freq_hz (0 if out of range)."""
    if result.magnitude_v.size == 0 or result.rbw_hz <= 0:
        return 0.0
    k = int(round(freq_hz / result.rbw_hz))
    if k < 0 or k >= result.magnitude_v.size:
        return 0.0
    return float(result.magnitude_v[k])


def thd(result: SpectrumResult, fundamental_hz: float, n_harmonics: int = 5) -> float:
    """Total harmonic distortion = sqrt(sum V_n^2, n>=2) / V_1, using the nearest bins
    to each harmonic (coherent capture assumed). Harmonics above Nyquist are skipped."""
    v1 = _bin_amp(result, fundamental_hz)
    if v1 <= 0:
        return float("nan")
    nyq = result.frequency_hz[-1] if result.frequency_hz.size else 0.0
    harm_sq = 0.0
    for k in range(2, n_harmonics + 1):
        fk = k * fundamental_hz
        if fk > nyq:
            break
        harm_sq += _bin_amp(result, fk) ** 2
    return float(np.sqrt(harm_sq) / v1)


def snr_db(result: SpectrumResult, fundamental_hz: float) -> float:
    """20log10(V_1 / noise_rms), where noise = RMS of all non-DC bins excluding the
    fundamental's +/-1-bin neighborhood. Returns +inf for a perfectly clean synthetic."""
    v1 = _bin_amp(result, fundamental_hz)
    mag = result.magnitude_v
    if v1 <= 0 or mag.size < 2 or result.rbw_hz <= 0:
        return float("nan")
    k1 = int(round(fundamental_hz / result.rbw_hz))
    mask = np.ones(mag.size, dtype=bool)
    mask[0] = False  # DC
    for j in (k1 - 1, k1, k1 + 1):
        if 0 <= j < mag.size:
            mask[j] = False
    noise = float(np.sqrt(np.sum(mag[mask] ** 2)))
    if noise <= 0:
        return float("inf")
    return float(20.0 * np.log10(v1 / noise))


def bode_f3db(
    freq_hz: np.ndarray | list[float],
    gain_db: np.ndarray | list[float],
) -> dict[str, float | None]:
    """From a low-pass Bode sweep: the -3 dB frequency (relative to passband gain at
    the lowest frequency) by log-frequency interpolation, plus the high-side rolloff
    slope in dB/decade. Returns f_3db_hz=None when gain never drops 3 dB below
    passband over the swept range."""
    f = np.asarray(freq_hz, dtype=float)
    g = np.asarray(gain_db, dtype=float)
    order = np.argsort(f)
    f, g = f[order], g[order]
    passband = float(g[0])
    target = passband - 3.0102999566  # 20log10(1/sqrt2)
    f3db: float | None = None
    for i in range(1, f.size):
        if g[i] <= target <= g[i - 1] or g[i - 1] <= target <= g[i]:
            lf0, lf1 = np.log10(f[i - 1]), np.log10(f[i])
            g0, g1 = g[i - 1], g[i]
            if g1 != g0:
                t = (target - g0) / (g1 - g0)
                f3db = float(10 ** (lf0 + t * (lf1 - lf0)))
            else:
                f3db = float(f[i])
            break
    if f.size >= 2 and f[-1] > f[-2]:
        slope = (g[-1] - g[-2]) / (np.log10(f[-1]) - np.log10(f[-2]))
    else:
        slope = float("nan")
    return {"f_3db_hz": f3db, "rolloff_db_per_decade": float(slope)}
