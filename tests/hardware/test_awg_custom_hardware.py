"""Hardware validation for awg.upload_custom (arbitrary waveform output) — a path
that shipped broken (the backend passed a Python list to pydwf's nodeDataSet, which
wants an ndarray, and never set the Custom function or applied amplitude) because the
FakeBackend just records the samples and no hardware test exercised it.

Device-conditional wiring via the `analog_loopback` fixture: on the AD3 the W1->CH1
loopback is auto-routed through the Jumperless (autonomous); on the ADP2230 (analog on
BNC) it needs a manual cable opted-in via ADP_AWG_SCOPE_CHANNELS.

Run: DWF_TEST_SERIAL=<serial> pytest tests/hardware/test_awg_custom_hardware.py -m hardware -v
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"awg", "scope"})
def test_upload_custom_plays_scaled_waveform(device, artifacts, analog_loopback) -> None:
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.scope import Scope

    awg_ch, scope_ch = analog_loopback
    awg = AWG(device=device, artifacts=artifacts)
    scope = Scope(device=device, artifacts=artifacts)

    n = 4096
    rep_hz = 1_000.0            # custom-buffer repetition rate (set via configure)
    amp = 1.0                   # peak amplitude requested at upload_custom
    # Two full sine cycles over the custom buffer => the played signal is a 2 kHz sine
    # (2 cycles x 1 kHz repetition). A plain Sine would be 1 kHz, so the frequency check
    # proves the *custom* data is what's playing.
    samples = np.sin(2 * np.pi * 2 * np.arange(n) / n).astype(np.float64)

    # configure sets the playback rate (documented: configure separately for rate). The
    # deliberately-wrong amplitude here must be overridden by upload_custom's amplitude_v.
    awg.configure(channel=awg_ch, function="Custom", frequency_hz=rep_hz, amplitude_v=0.3)
    awg.upload_custom(channel=awg_ch, samples_npy_path=None, amplitude_v=amp, _samples=samples)
    try:
        awg.start(channel=awg_ch)
        scope.configure(channels=[scope_ch], range_v=5.0, offset_v=0.0, coupling="DC",
                        sample_rate_hz=200_000, buffer_size=4096)
        s = scope.capture()["summary"][f"ch{scope_ch}"]
    finally:
        awg.stop(channel=awg_ch)

    freq = s["freq_estimate"]
    peak_to_peak = s["max"] - s["min"]
    assert abs(freq - 2 * rep_hz) / (2 * rep_hz) < 0.1, (
        f"freq {freq:.1f} Hz != ~2000 Hz — custom waveform not playing (function not Custom?)"
    )
    assert 1.6 < peak_to_peak < 2.4, (
        f"peak-to-peak {peak_to_peak:.3f} V != ~2.0 V — amplitude_v not applied to hardware"
    )
