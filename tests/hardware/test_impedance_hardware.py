"""Impedance hardware validation. Needs an analog device with W1 -> R_ref -> DUT -> GND,
CH1 across the whole network (V_total) and CH2 across the DUT (V_dut). The Jumperless
can't route this network, so it is opt-in via env, like the Bode through-wire test.

Wiring + env:
  Known-R release gate:  R_ref and DUT both ~1 kOhm. Set
      IMPEDANCE_RREF_OHMS=1000  IMPEDANCE_R_DUT_OHMS=1000
  Known-C:               R_ref ~1 kOhm, DUT a film cap. Set additionally
      IMPEDANCE_C_DUT_F=1e-7   (100 nF)

Run:
  DWF_TEST_SERIAL=210415BB5F2A IMPEDANCE_RREF_OHMS=1000 IMPEDANCE_R_DUT_OHMS=1000 \\
    .venv/bin/pytest tests/hardware/test_impedance_hardware.py -m hardware -v
"""
from __future__ import annotations

import os

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"impedance"})
@pytest.mark.skipif(
    "IMPEDANCE_RREF_OHMS" not in os.environ or "IMPEDANCE_R_DUT_OHMS" not in os.environ,
    reason="set IMPEDANCE_RREF_OHMS + IMPEDANCE_R_DUT_OHMS with a known-R DUT wired",
)
def test_impedance_known_resistor_release_gate(device, artifacts) -> None:
    """Known resistor DUT -> flat |Z| ~ R_dut, phase ~ 0 across the sweep. Release gate."""
    r_ref = float(os.environ["IMPEDANCE_RREF_OHMS"])
    r_dut = float(os.environ["IMPEDANCE_R_DUT_OHMS"])
    from dwf_mcp.instruments.impedance import Impedance

    imp = Impedance(device=device, artifacts=artifacts)
    imp.configure(start_hz=100.0, stop_hz=100_000.0, points=15, spacing="log",
                  amplitude_v=0.5, r_ref=r_ref, ref_channel=1, dut_channel=2)
    out = imp.measure()
    npz = np.load(out["path"])
    z = npz["impedance_ohms"]
    ph = npz["phase_deg"]
    assert np.median(z) == pytest.approx(r_dut, rel=0.10), (
        f"|Z| median {np.median(z):.1f} != {r_dut}"
    )
    assert np.max(np.abs(ph)) < 5.0, f"phase not flat: {ph}"


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"impedance"})
@pytest.mark.skipif(
    "IMPEDANCE_C_DUT_F" not in os.environ or "IMPEDANCE_RREF_OHMS" not in os.environ,
    reason="set IMPEDANCE_RREF_OHMS + IMPEDANCE_C_DUT_F with a known cap DUT wired",
)
def test_impedance_known_capacitor(device, artifacts) -> None:
    """Known capacitor DUT -> |Z| = 1/(2*pi*f*C), phase ~ -90, recovered C ~ C_dut.
    Sweep is centered so |Z| straddles R_ref for good conditioning."""
    r_ref = float(os.environ["IMPEDANCE_RREF_OHMS"])
    c_dut = float(os.environ["IMPEDANCE_C_DUT_F"])
    fc = 1.0 / (2 * np.pi * r_ref * c_dut)   # freq where |Z| == r_ref
    from dwf_mcp.instruments.impedance import Impedance

    imp = Impedance(device=device, artifacts=artifacts)
    imp.configure(start_hz=fc / 10, stop_hz=fc * 10, points=21, spacing="log",
                  amplitude_v=0.5, r_ref=r_ref, ref_channel=1, dut_channel=2)
    out = imp.measure()
    npz = np.load(out["path"])
    freqs = npz["frequency_hz"]
    z = npz["impedance_ohms"]
    cap = npz["capacitance_f"]
    ideal = 1.0 / (2 * np.pi * freqs * c_dut)
    rel_err = np.abs(z - ideal) / ideal
    assert np.median(rel_err) < 0.10, f"median |Z| rel err {np.median(rel_err):.3f}"
    assert np.nanmedian(npz["phase_deg"]) == pytest.approx(-90.0, abs=8.0)
    assert np.nanmedian(cap) == pytest.approx(c_dut, rel=0.15)
