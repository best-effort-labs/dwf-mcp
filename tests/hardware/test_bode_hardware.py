"""Bode hardware validation. Needs an analog device with W1 driving CH1 (Vin) and
the DUT output on CH2 (Vout). Like the spectrum AWG→scope test, this requires a
physical cable that the Jumperless can't route, so it is **opt-in** via the same
env var used by the spectrum and ADP2230 AWG→scope tests:

  DWF_TEST_SERIAL=<serial> ADP_AWG_SCOPE_CHANNELS=1 \\
    .venv/bin/pytest tests/hardware/test_bode_hardware.py -m hardware -v

Wiring:
  Through-wire: tie CH2 directly to CH1 (+W1). This confirms ratiometric math and
                timing skew. This is the **release gate** for the bode instrument.
  RC low-pass:  W1 -> R -> node, node -> C -> GND, CH1 probes W1 output (Vin),
                CH2 probes the node (Vout). Set BODE_RC_FC_HZ to 1/(2*pi*R*C) Hz
                to enable the RC assertion.

Marked `wired` (so `-m "not wired"` excludes it) but does NOT use the Jumperless;
the env opt-in (ADP_AWG_SCOPE_CHANNELS) is the gate for through-wire and readback
tests, and BODE_RC_FC_HZ is the gate for the RC test.
"""
from __future__ import annotations

import contextlib
import os

import numpy as np
import pytest


def _parse_cabled_channels() -> set[int]:
    """Parse ADP_AWG_SCOPE_CHANNELS (e.g. "1", "2", "1,2") into a set of ints."""
    out: set[int] = set()
    for tok in os.environ.get("ADP_AWG_SCOPE_CHANNELS", "").replace(",", " ").split():
        with contextlib.suppress(ValueError):
            out.add(int(tok))
    return out


_CABLED_CHANNELS = _parse_cabled_channels()


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"bode"})
def test_bode_through_wire_flat(device, artifacts) -> None:
    """Drive W1 into CH1 (Vin) with CH2 tied to CH1 (through-wire); confirm the
    Bode sweep is flat (0 dB / 0 deg) across 100 Hz – 100 kHz. This is the
    ratiometric-math + timing-skew release gate for the bode instrument."""
    if 1 not in _CABLED_CHANNELS:
        pytest.skip(
            "W1 not cabled to CH1; set ADP_AWG_SCOPE_CHANNELS=1 (or include 1) "
            "and tie CH2 to CH1 to run the through-wire release gate"
        )
    from dwf_mcp.instruments.bode import Bode

    bode = Bode(device=device, artifacts=artifacts)
    bode.configure(
        start_hz=100.0,
        stop_hz=100_000.0,
        points=15,
        spacing="log",
        amplitude_v=0.5,
        ref_channel=1,
        dut_channel=2,
    )
    out = bode.measure()
    npz = np.load(out["path"])
    gains, phases = npz["gain_db"], npz["phase_deg"]
    assert np.max(np.abs(gains)) < 0.5, f"through-wire gain not flat: {gains}"
    assert np.max(np.abs(phases)) < 2.0, f"through-wire phase not flat: {phases}"


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"bode"})
@pytest.mark.skipif(
    "BODE_RC_FC_HZ" not in os.environ,
    reason="set BODE_RC_FC_HZ=<1/(2*pi*R*C)> with an RC low-pass wired between W1/CH1/CH2",
)
def test_bode_rc_lowpass_corner(device, artifacts) -> None:
    """Sweep an RC low-pass filter; confirm −3 dB & −45 deg at f_c and rolloff above.
    Requires: W1 -> R -> node, CH1 at W1, CH2 at node, node -> C -> GND.
    BODE_RC_FC_HZ must be set to the computed corner frequency 1/(2*pi*R*C)."""
    fc = float(os.environ["BODE_RC_FC_HZ"])
    from dwf_mcp.instruments.bode import Bode

    bode = Bode(device=device, artifacts=artifacts)
    bode.configure(
        start_hz=fc / 50,
        stop_hz=fc * 50,
        points=31,
        spacing="log",
        amplitude_v=0.5,
        ref_channel=1,
        dut_channel=2,
    )
    out = bode.measure()
    npz = np.load(out["path"])
    freqs, gains, phases = npz["frequency_hz"], npz["gain_db"], npz["phase_deg"]
    i = int(np.argmin(np.abs(freqs - fc)))
    assert gains[i] == pytest.approx(-3.0, abs=1.0), (
        f"corner gain: expected -3 dB at f_c={fc} Hz, got {gains[i]:.2f} dB"
    )
    assert phases[i] == pytest.approx(-45.0, abs=8.0), (
        f"corner phase: expected -45 deg at f_c={fc} Hz, got {phases[i]:.1f} deg"
    )
    assert gains[-1] < gains[0] - 15.0, (
        f"no rolloff: high-end gain {gains[-1]:.2f} dB not 15 dB below low-end {gains[0]:.2f} dB"
    )


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"bode"})
def test_bode_readback_contract(device, artifacts) -> None:  # noqa: ARG001
    """Empirical contract: awg_frequency_get and scope_sample_rate_get must return
    what the hardware ACTUALLY uses for an awkward (non-round) request, not the
    raw requested value. Quantization is expected to be small (< 1 % / 5 %)."""
    if 1 not in _CABLED_CHANNELS:
        pytest.skip(
            "W1 not cabled to CH1; set ADP_AWG_SCOPE_CHANNELS=1 to run readback contract"
        )
    be = device.backend
    be.awg_configure(
        channel=1,
        function="Sine",
        freq_hz=12_345.678,
        amplitude_v=0.5,
        offset_v=0.0,
        phase_deg=0.0,
        symmetry=50.0,
        run_time_s=None,
    )
    f_act = be.awg_frequency_get(1)
    assert f_act > 0 and abs(f_act - 12_345.678) / 12_345.678 < 0.01, (
        f"awg_frequency_get returned {f_act!r}; expected ~12345.678 Hz (< 1 % error)"
    )
    be.scope_set_acquisition(sample_rate_hz=789_321.0, buffer_size=2048, mode="Single")
    sr_act = be.scope_sample_rate_get()
    assert sr_act > 0 and abs(sr_act - 789_321.0) / 789_321.0 < 0.05, (
        f"scope_sample_rate_get returned {sr_act!r}; expected ~789321 Hz (< 5 % error)"
    )
    be.awg_stop(channel=1)
