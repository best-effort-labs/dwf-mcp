from __future__ import annotations

import math

import numpy as np
import pytest

from dwf_mcp.impedance_dsp import derive_components, impedance_point


def _divider_channels(z_dut: complex, r_ref: float, freq: float, sr: float, n: int,
                      amp: float = 1.0):
    """Forward synthesis (independent of impedance_point): drive=V_total on CH-total,
    V_dut = V_total * Z/(Z+R_ref) on CH-dut. Returns (v_total, v_dut) sample arrays."""
    t = np.arange(n) / sr
    drive = amp * np.cos(2 * np.pi * freq * t)
    h = z_dut / (z_dut + r_ref)               # complex divider transfer
    v_dut = amp * abs(h) * np.cos(2 * np.pi * freq * t + np.angle(h))
    return drive, v_dut


def test_pure_resistor_flat_zero_phase():
    sr, n, r_ref = 1_000_000.0, 8192, 1000.0
    freq = 32 * sr / n
    v_total, v_dut = _divider_channels(complex(1000.0, 0.0), r_ref, freq, sr, n)
    out = impedance_point(v_total, v_dut, sr, freq, r_ref)
    assert out["impedance_ohms"] == pytest.approx(1000.0, rel=1e-3)
    assert out["phase_deg"] == pytest.approx(0.0, abs=0.1)
    assert out["resistance_ohms"] == pytest.approx(1000.0, rel=1e-3)
    assert out["reactance_ohms"] == pytest.approx(0.0, abs=1.0)
    assert math.isnan(out["capacitance_f"]) and math.isnan(out["inductance_f"])


def test_pure_capacitor_magnitude_and_minus90_phase():
    sr, n, r_ref, c = 1_000_000.0, 8192, 1000.0, 100e-9
    freq = 32 * sr / n
    z = complex(0.0, -1.0 / (2 * np.pi * freq * c))
    v_total, v_dut = _divider_channels(z, r_ref, freq, sr, n)
    out = impedance_point(v_total, v_dut, sr, freq, r_ref)
    assert out["impedance_ohms"] == pytest.approx(abs(z), rel=2e-3)
    assert out["phase_deg"] == pytest.approx(-90.0, abs=0.5)
    assert out["capacitance_f"] == pytest.approx(c, rel=5e-3)
    assert math.isnan(out["inductance_f"])


def test_pure_inductor_plus90_phase_and_L():
    sr, n, r_ref, ll = 1_000_000.0, 8192, 100.0, 1e-3
    freq = 64 * sr / n
    z = complex(0.0, 2 * np.pi * freq * ll)
    v_total, v_dut = _divider_channels(z, r_ref, freq, sr, n)
    out = impedance_point(v_total, v_dut, sr, freq, r_ref)
    assert out["phase_deg"] == pytest.approx(90.0, abs=0.5)
    assert out["inductance_f"] == pytest.approx(ll, rel=5e-3)
    assert math.isnan(out["capacitance_f"])


def test_series_rc_recovers_esr_and_capacitance():
    sr, n, r_ref, r, c = 1_000_000.0, 8192, 1000.0, 220.0, 100e-9
    freq = 32 * sr / n
    z = complex(r, -1.0 / (2 * np.pi * freq * c))
    v_total, v_dut = _divider_channels(z, r_ref, freq, sr, n)
    out = impedance_point(v_total, v_dut, sr, freq, r_ref)
    assert out["resistance_ohms"] == pytest.approx(r, rel=1e-2)
    assert out["capacitance_f"] == pytest.approx(c, rel=1e-2)


def test_low_drive_guard_when_current_floored():
    # V_total == V_dut -> no voltage across R_ref -> current floored -> NaN Z, no crash.
    sr, n, r_ref = 1_000_000.0, 4096, 1000.0
    freq = 16 * sr / n
    t = np.arange(n) / sr
    same = np.cos(2 * np.pi * freq * t)
    out = impedance_point(same, same.copy(), sr, freq, r_ref)
    assert math.isnan(out["impedance_ohms"])
    assert out["drive_rms"] == pytest.approx(0.0, abs=1e-9)


def test_derive_components_passivity_guard_negative_R():
    # Negative R (noise) -> Q and D are NaN, not nonsensical.
    out = derive_components(resistance_ohms=-5.0, reactance_ohms=-300.0, freq_hz=1000.0)
    assert math.isnan(out["q_factor"]) and math.isnan(out["dissipation"])


def test_derive_components_zero_reactance_dissipation_nan():
    out = derive_components(resistance_ohms=1000.0, reactance_ohms=0.0, freq_hz=1000.0)
    assert math.isnan(out["dissipation"])           # R/|X|, X==0
    assert math.isnan(out["capacitance_f"]) and math.isnan(out["inductance_f"])
