from __future__ import annotations

from dwf_mcp import bode_dsp, sweep_dsp


def test_qf_bits_unique_and_single_bit():
    bits = [v for k, v in vars(sweep_dsp).items()
            if k.startswith("QF_") and k != "QF_NAMES" and isinstance(v, int)]
    assert len(bits) >= 13
    assert len(bits) == len(set(bits)), "duplicate QF_* bit value"
    for b in bits:
        assert b != 0 and (b & (b - 1)) == 0, f"{b} is not a single bit"


def test_qf_names_cover_all_bits():
    bits = {v for k, v in vars(sweep_dsp).items()
            if k.startswith("QF_") and k != "QF_NAMES" and isinstance(v, int)}
    assert bits == set(sweep_dsp.QF_NAMES), "every flag bit must have a name"


def test_bode_dsp_reexports_are_the_sweep_dsp_objects():
    assert bode_dsp.extract_tone is sweep_dsp.extract_tone
    assert bode_dsp.plan_acquisition is sweep_dsp.plan_acquisition
    assert bode_dsp.assess_quality is sweep_dsp.assess_quality
    assert bode_dsp.QF_NAMES is sweep_dsp.QF_NAMES
    assert bode_dsp.QF_LOW_VIN_RMS == sweep_dsp.QF_LOW_VIN_RMS
    assert bode_dsp.bode_point.__module__ == "dwf_mcp.bode_dsp"
