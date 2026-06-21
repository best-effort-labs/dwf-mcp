# tests/unit/test_formulae_f3db.py
from __future__ import annotations

import numpy as np

from dwf_mcp import formulae


def _onepole_lowpass_gain_db(freqs: np.ndarray, fc: float) -> np.ndarray:
    return 20.0 * np.log10(1.0 / np.sqrt(1.0 + (freqs / fc) ** 2))


def test_f3db_recovers_corner() -> None:
    fc = 1591.5
    freqs = np.logspace(1, 5, 60)  # 10 Hz .. 100 kHz
    gain = _onepole_lowpass_gain_db(freqs, fc)
    out = formulae.bode_f3db(freqs, gain)
    f3 = out["f_3db_hz"]
    assert f3 is not None
    assert abs(f3 - fc) / fc < 0.05
    rolloff = out["rolloff_db_per_decade"]
    assert rolloff is not None
    assert -22.0 < rolloff < -18.0


def test_f3db_none_when_no_crossing() -> None:
    freqs = np.logspace(1, 3, 20)
    gain = np.zeros_like(freqs)  # flat, never drops 3 dB
    out = formulae.bode_f3db(freqs, gain)
    assert out["f_3db_hz"] is None
