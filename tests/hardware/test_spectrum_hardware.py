"""Spectrum (FFT) hardware validation: AWG sine -> spectrum.measure() reads the
right peak frequency + amplitude over a physical W1 -> scope CHn BNC cable.

Like the AWG->scope analog check, this needs a BNC cable the Jumperless can't
route, so it's **opt-in** via the same env var:
  ADP_AWG_SCOPE_CHANNELS=1            # W1 -> CH1
  ADP_AWG_SCOPE_CHANNELS=2            # W1 -> CH2
  ADP_AWG_SCOPE_CHANNELS=1,2         # both
Unset (default) -> skips. Marked `wired` (so `-m "not wired"` excludes it) but it
does NOT use the Jumperless, so the no-Jumperless auto-skip does not apply — the
env opt-in is its only gate. Works on any analog DUT (AD3, ADP2230).

Run: DWF_TEST_SERIAL=210415BB5F2A ADP_AWG_SCOPE_CHANNELS=1 \\
     .venv/bin/pytest tests/hardware/test_spectrum_hardware.py -m hardware -v
"""
from __future__ import annotations

import contextlib
import os
import time

import pytest


def _parse_cabled_channels() -> set[int]:
    out: set[int] = set()
    for tok in os.environ.get("ADP_AWG_SCOPE_CHANNELS", "").replace(",", " ").split():
        with contextlib.suppress(ValueError):
            out.add(int(tok))
    return out


_CABLED_CHANNELS = _parse_cabled_channels()


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"awg", "spectrum"})
@pytest.mark.parametrize("scope_ch", [1, 2])
def test_spectrum_reads_awg_tone(device, artifacts, scope_ch) -> None:
    """Drive W1 with a 10 kHz, 1 V-peak sine; confirm the spectrum peak lands at
    ~10 kHz and ~-3 dBV (rms; 1 V peak = 0.707 Vrms), well above the noise floor."""
    if scope_ch not in _CABLED_CHANNELS:
        pytest.skip(
            f"scope CH{scope_ch} not opted in; set ADP_AWG_SCOPE_CHANNELS "
            f"(e.g. '1', '2', '1,2') once W1 is cabled to that scope input"
        )
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.spectrum import Spectrum

    awg = AWG(device=device, artifacts=artifacts)
    spec = Spectrum(device=device, artifacts=artifacts)
    try:
        awg.configure(channel=1, function="Sine", frequency_hz=10_000.0, amplitude_v=1.0)
        awg.start(channel=1)
        time.sleep(0.3)  # let the AWG output settle
        spec.configure(channel=scope_ch, sample_rate_hz=1_000_000.0, buffer_size=16384,
                       window="flattop", amplitude="rms")
        # measure() auto-discards the stale post-open AnalogIn buffer (discard_warmup
        # defaults True), so a single call returns clean data even right after open.
        s = spec.measure()["summary"]
    finally:
        awg.stop(channel=1)

    assert 9_500 < s["peak_frequency_hz"] < 10_500, s
    # amplitude_v=1.0 is the peak amplitude (~2 Vpp) -> 0.707 Vrms -> ~-3.0 dBV.
    assert -5.0 < s["peak_magnitude_dbv"] < -1.0, s
    assert s["noise_floor_dbv"] < s["peak_magnitude_dbv"] - 20  # peak well above floor
