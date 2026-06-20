"""Bode-specific DSP: the ratiometric gain/phase point. The generic single-tone
sweep primitives now live in `sweep_dsp`; re-exported here so existing
`from dwf_mcp.bode_dsp import extract_tone, ...` call sites keep working."""
from __future__ import annotations

import numpy as np

from dwf_mcp.sweep_dsp import (  # noqa: F401  (re-exported for back-compat)
    COHERENCE_TOL_CYCLES,
    QF_BUFFER_LIMITED,
    QF_CLIPPED,
    QF_LOW_CYCLES,
    QF_LOW_DRIVE,
    QF_LOW_DUT_VOLTAGE,
    QF_LOW_OVERSAMPLING,
    QF_LOW_SAMPLES_PER_CYCLE,
    QF_LOW_VIN_RMS,
    QF_NAMES,
    QF_NEAR_NYQUIST,
    QF_NONCOHERENT,
    QF_REF_MISMATCH,
    QF_SAMPLE_RATE_LIMITED,
    QF_VERY_LOW_SAMPLES_PER_CYCLE,
    AcqPlan,
    assess_quality,
    detect_clip,
    extract_tone,
    frequency_grid,
    plan_acquisition,
)

_FLOOR = 1e-15


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
