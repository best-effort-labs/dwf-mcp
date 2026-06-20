"""Pure DSP for the impedance analyzer — no hardware, no I/O. Recovers complex
impedance from a series-reference-resistor divider: given V_total (across the whole
W1->R_ref->DUT->GND network) and V_dut (across the DUT), the shared series current is
I = (V_total - V_dut)/R_ref and Z = V_dut / I. Derived component values are the
series-equivalent forms (Cs/Ls/Q/D)."""
from __future__ import annotations

import numpy as np

from dwf_mcp.sweep_dsp import extract_tone

_FLOOR = 1e-15


def derive_components(resistance_ohms: float, reactance_ohms: float,
                      freq_hz: float) -> dict[str, float]:
    """Series-equivalent Cs/Ls/Q/D from Z = R + jX. Capacitance only for X<0,
    inductance only for X>0 (else NaN, with a _FLOOR deadband so a pure resistor's
    numerical-residue reactance reads as NaN, not a spurious component). Q=|X|/R and
    D=R/|X| are both NaN when R<=0 (a passive DUT has R>=0; negative R is measurement
    noise); additionally D is NaN when |X|<=_FLOOR (Q is simply 0 there)."""
    r = float(resistance_ohms)
    x = float(reactance_ohms)
    w = 2.0 * np.pi * float(freq_hz)
    nan = float("nan")
    capacitance_f = -1.0 / (w * x) if (x < -_FLOOR and w > 0.0) else nan
    inductance_f = x / w if (x > _FLOOR and w > 0.0) else nan
    if r <= 0.0:
        q_factor = nan
        dissipation = nan
    else:
        q_factor = abs(x) / r
        dissipation = r / abs(x) if abs(x) > _FLOOR else nan
    return {"capacitance_f": float(capacitance_f), "inductance_f": float(inductance_f),
            "q_factor": float(q_factor), "dissipation": float(dissipation)}


def impedance_point(v_total: np.ndarray, v_dut: np.ndarray,
                    sample_rate_hz: float, freq_hz: float, r_ref: float) -> dict[str, float]:
    """Complex-impedance point from the divider voltages. Returns |Z|, phase (deg,
    (-180,180]), R, X, the derived series-equivalent C/L/Q/D, the three RMS guardrail
    magnitudes (v_total_rms, v_dut_rms, drive_rms = |V_total - V_dut|). When the current
    is floored (V_total ~ V_dut) Z is NaN and the caller raises `low_drive`."""
    h_total = extract_tone(v_total, sample_rate_hz, freq_hz)
    h_dut = extract_tone(v_dut, sample_rate_hz, freq_hz)
    v_total_rms = float(abs(h_total))
    v_dut_rms = float(abs(h_dut))
    h_drive = h_total - h_dut          # phasor across R_ref (proportional to current)
    drive_rms = float(abs(h_drive))
    nan = float("nan")
    if drive_rms <= _FLOOR:
        return {"impedance_ohms": nan, "phase_deg": nan,
                "resistance_ohms": nan, "reactance_ohms": nan,
                "capacitance_f": nan, "inductance_f": nan,
                "q_factor": nan, "dissipation": nan,
                "v_total_rms": v_total_rms, "v_dut_rms": v_dut_rms, "drive_rms": drive_rms}
    current = h_drive / r_ref
    z = h_dut / current                # = h_dut * r_ref / (h_total - h_dut)
    resistance_ohms = float(z.real)
    reactance_ohms = float(z.imag)
    comps = derive_components(resistance_ohms, reactance_ohms, freq_hz)
    return {"impedance_ohms": float(abs(z)),
            "phase_deg": float(np.degrees(np.angle(z))),
            "resistance_ohms": resistance_ohms, "reactance_ohms": reactance_ohms,
            **comps,
            "v_total_rms": v_total_rms, "v_dut_rms": v_dut_rms, "drive_rms": drive_rms}
