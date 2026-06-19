"""Pure DSP for the Bode/network analyzer instrument — no hardware, no I/O.
Coherent-first single-bin extraction: a rectangular complex projection at the
EXACT drive frequency (no window). On a coherent capture this is the unbiased
DFT bin; off-coherent points are FLAGGED (assess_quality), not windowed."""
from __future__ import annotations

import numpy as np

_FLOOR = 1e-15  # amplitude/ratio floor so log10 never sees 0


def extract_tone(samples: np.ndarray, sample_rate_hz: float, freq_hz: float) -> complex:
    """Single-bin complex amplitude at exactly freq_hz, returned as a **Vrms phasor**
    (magnitude = Vrms, angle = phase, cosine reference). Rectangular projection:
    (2/N)*sum x[n]*exp(-j2*pi*f*n/fs) is the one-sided complex peak; /sqrt(2) -> Vrms."""
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n == 0:
        return 0j
    k = freq_hz / sample_rate_hz  # cycles per sample
    nn = np.arange(n)
    proj = np.dot(x, np.exp(-2j * np.pi * k * nn))
    peak = 2.0 * proj / n
    return complex(peak / np.sqrt(2.0))


def bode_point(vin: np.ndarray, vout: np.ndarray,
               sample_rate_hz: float, freq_hz: float) -> dict[str, float]:
    """gain_db = 20log10(|Vout|/|Vin|), phase_deg = angle(Vout/Vin) in (-180,180]
    (negative = lag). Ratiometric: AWG amplitude error and the absolute timing/phase
    reference cancel. Vrms values are absolute (for guardrails)."""
    h_in = extract_tone(vin, sample_rate_hz, freq_hz)
    h_out = extract_tone(vout, sample_rate_hz, freq_hz)
    vin_rms = float(abs(h_in))
    vout_rms = float(abs(h_out))
    if vin_rms <= _FLOOR:
        # Reference is dead: gain undefined. Floor it; the caller flags low_vin_rms.
        return {"gain_db": 20.0 * np.log10(_FLOOR), "phase_deg": 0.0,
                "vin_rms": vin_rms, "vout_rms": vout_rms}
    ratio = h_out / h_in
    gain_db = float(20.0 * np.log10(max(abs(ratio), _FLOOR)))
    phase_deg = float(np.degrees(np.angle(ratio)))  # np.angle -> (-pi, pi] -> (-180, 180]
    return {"gain_db": gain_db, "phase_deg": phase_deg,
            "vin_rms": vin_rms, "vout_rms": vout_rms}
