"""Opt-in hardware confirmation tests for the cookbook formulae (THD + Bode f_3dB).

These tests validate that the pure formulae in `dwf_mcp.formulae` agree with
real hardware measurements. Neither test uses the Jumperless (can't route BNC);
both are gated exclusively on env vars.

TASK 13 — square-wave THD (standalone, AWG loopback)
-----------------------------------------------------
Gate: ADP_AWG_SCOPE_CHANNELS=<awg_ch>,<scope_ch>  (e.g. "1,1" or "1")
Wiring: W1 looped back to scope CH<n> via a short BNC cable or a direct patch.
Run:
  DWF_TEST_SERIAL=<serial> ADP_AWG_SCOPE_CHANNELS=1 \\
    .venv/bin/pytest tests/hardware/test_cookbook_formulae_hardware.py \\
    -k test_square_wave_thd -m hardware -v

TASK 14 — RC f_3dB confirm (wired, passive DUT)
-----------------------------------------------
Gate: ADP_AWG_SCOPE_CHANNELS=1  AND  BODE_RC_FC_HZ=<1/(2*pi*R*C)>
Wiring: same RC low-pass rig as test_bode_hardware.py:
  W1 -> R(1 kΩ) -> node;  node -> C(100 nF) -> GND;
  CH1 probes W1 output (Vin);  CH2 probes node (Vout).
  R=1kΩ + C=100nF → f_c ≈ 1591.5 Hz.
Run:
  DWF_TEST_SERIAL=<serial> ADP_AWG_SCOPE_CHANNELS=1 BODE_RC_FC_HZ=1591.5 \\
    .venv/bin/pytest tests/hardware/test_cookbook_formulae_hardware.py \\
    -k test_rc_f3db -m hardware -v
"""
from __future__ import annotations

import contextlib
import os
import time

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_cabled_channels() -> set[int]:
    """Parse ADP_AWG_SCOPE_CHANNELS (e.g. "1", "2", "1,2") into a set of ints."""
    out: set[int] = set()
    for tok in os.environ.get("ADP_AWG_SCOPE_CHANNELS", "").replace(",", " ").split():
        with contextlib.suppress(ValueError):
            out.add(int(tok))
    return out


_CABLED_CHANNELS = _parse_cabled_channels()


# ---------------------------------------------------------------------------
# TASK 13: Square-wave THD hardware confirmation
# ---------------------------------------------------------------------------

@pytest.mark.hardware
@pytest.mark.standalone
@pytest.mark.requires(instruments={"awg", "scope"})
def test_square_wave_thd_matches_theory(device, artifacts) -> None:
    """Drive W1 with a 1 kHz square wave, capture coherently, compute THD via
    the cookbook formula, and assert it falls in [0.30, 0.55].

    Theory: an ideal square wave has odd harmonics at relative amplitudes
    1, 1/3, 1/5, 1/7, ... giving THD ≈ sqrt(1/9 + 1/25 + ...) ≈ 0.483 for
    all harmonics.  With a finite bandwidth (scope/AWG combined) the high
    harmonics are attenuated, pulling THD down toward ~0.30. We therefore
    gate on the open interval (0.30, 0.55) — wide enough for any supported
    Digilent device/config, tight enough to catch a sine driving a square-wave
    THD formula, or a constant-zero result from a wiring failure.

    Coherent capture: buffer_size = integer × (sample_rate / fundamental_hz)
    so rectangular windowing bins each harmonic exactly on one bin, avoiding
    spectral leakage that would suppress the harmonic peak.
    """
    if not _CABLED_CHANNELS:
        pytest.skip(
            "no scope channel opted in; set ADP_AWG_SCOPE_CHANNELS=<n> once "
            "W1 is looped back to scope CH<n> (e.g. ADP_AWG_SCOPE_CHANNELS=1)"
        )
    # Use the first cabled channel for the loopback.
    scope_ch = min(_CABLED_CHANNELS)

    from dwf_mcp import formulae
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.scope import Scope
    from dwf_mcp.spectrum_dsp import compute_spectrum

    fundamental_hz = 1_000.0
    # 100× oversampling (sample_rate / fundamental = 100 cycles per buffer).
    # buffer_size must be an integer number of periods: 100 periods × 100
    # samples/period = 10 000 samples.  That gives rbw = 10 Hz, so harmonic
    # bins k=1,3,5,...  land exactly on integer bin indices.
    sample_rate_hz = 100_000.0
    cycles_in_buffer = 100                     # integer number of fundamental periods
    buffer_size = int(sample_rate_hz / fundamental_hz * cycles_in_buffer)  # 10 000

    awg = AWG(device=device, artifacts=artifacts)
    scope = Scope(device=device, artifacts=artifacts)
    try:
        # --- settle BEFORE arm: configure AWG first, let it run, THEN arm scope ---
        awg.configure(
            channel=1,
            function="Square",
            frequency_hz=fundamental_hz,
            amplitude_v=1.0,
            offset_v=0.0,
        )
        awg.start(channel=1)
        time.sleep(0.3)  # let the AWG output settle before arming

        # Readback-validate the actual sample rate the hardware uses.
        # NOTE: AD3/ADP2230 quantise the requested sample rate to the nearest
        # achievable divider; the readback may differ from 100 000 Hz by up to
        # ~1 %.  We read it back so the compute_spectrum call sees the REAL rate.
        be = device.backend
        be.scope_set_acquisition(
            sample_rate_hz=sample_rate_hz,
            buffer_size=buffer_size,
            mode="Single",
        )
        sr_actual = be.scope_sample_rate_get()
        # Validate: actual rate must be within 5 % of requested (mirrors readback
        # contract in test_bode_hardware.py).
        assert sr_actual > 0 and abs(sr_actual - sample_rate_hz) / sample_rate_hz < 0.05, (
            f"scope_sample_rate_get returned {sr_actual!r}; expected ~{sample_rate_hz} Hz"
        )

        # Configure scope after AWG is running (settle-before-arm).
        # Use a ±2 V range for a 1 V-amplitude (2 Vpp) square wave.
        scope.configure(
            channels=[scope_ch],
            range_v=2.0,
            sample_rate_hz=sample_rate_hz,
            buffer_size=buffer_size,
        )
        # Trigger on the rising edge — gives a clean coherent window start.
        scope.set_trigger(
            source="detector_analog_in",
            channel=scope_ch,
            level_v=0.0,
            condition="Rising",
            timeout_s=2.0,
        )

        # Warm-up discard: the first AnalogIn buffer after a device open is stale
        # (validated on ADP2230).  The Scope.capture() call arms + reads one buffer;
        # we discard its data and arm again for the actual measurement.
        _warmup = scope.capture()   # discard — warms up the AnalogIn engine
        del _warmup

        # Re-arm for the real measurement.
        result = scope.capture()
    finally:
        awg.stop(channel=1)

    # --- compute spectrum and evaluate THD ---
    npz = np.load(result["path"])
    samples = np.asarray(npz[f"ch{scope_ch}"], dtype=np.float64)

    spec_result = compute_spectrum(
        samples, sr_actual, window="rectangular", amplitude="rms"
    )
    thd_value = formulae.thd(spec_result, fundamental_hz=fundamental_hz, n_harmonics=9)

    # Ideal odd-harmonic square wave: THD = sqrt(1/9 + 1/25 + 1/49 + ...) ≈ 0.483.
    # Bandwidth roll-off (AWG output impedance × scope input capacitance, AD3 ≈ 25 MHz
    # combined) attenuates the 9th harmonic (9 kHz) and above, pulling THD down toward
    # ~0.30–0.40 in practice.  A result outside [0.30, 0.55] indicates:
    #   < 0.30: harmonics heavily suppressed (wrong channel, bandwidth < 3 kHz, or sine)
    #   > 0.55: clipping, glitch, or formula applied to a non-square signal.
    assert 0.30 < thd_value < 0.55, (
        f"square-wave THD={thd_value:.4f} outside expected range [0.30, 0.55]; "
        f"check wiring (W1 → CH{scope_ch}), AWG function=Square, and buffer coherence"
    )


# ---------------------------------------------------------------------------
# TASK 14: RC f_3dB hardware confirmation
# ---------------------------------------------------------------------------

@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"awg", "scope"})
@pytest.mark.skipif(
    "BODE_RC_FC_HZ" not in os.environ,
    reason=(
        "set BODE_RC_FC_HZ=<1/(2*pi*R*C)> with an RC low-pass wired between "
        "W1/CH1/CH2 (e.g. R=1kΩ + C=100nF → BODE_RC_FC_HZ=1591.5)"
    ),
)
def test_rc_f3db_matches_corner(device, artifacts) -> None:
    """Run a Bode sweep over an RC low-pass, extract f_3dB via the cookbook formula,
    and assert it's within 10 % of the expected corner frequency.

    Reuses the same RC rig as test_bode_hardware.py (1 kΩ + 0.1 µF → f_c ≈ 1591.5 Hz).
    Sweep spans [f_c/50 .. f_c*50] with 31 log-spaced points.

    The 10 % tolerance covers:
    - Component tolerances (1 % resistor + 10 % ceramic cap → ≤11 % on f_c)
    - Interpolation error from sparse log-frequency sampling near the corner
    """
    if 1 not in _CABLED_CHANNELS:
        pytest.skip(
            "W1 not cabled to CH1; set ADP_AWG_SCOPE_CHANNELS=1 (or include 1) "
            "and wire the RC network (W1->R->node->C->GND, CH1@W1, CH2@node)"
        )

    fc = float(os.environ["BODE_RC_FC_HZ"])

    from dwf_mcp import formulae
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
    freq_hz = npz["frequency_hz"]
    gain_db = npz["gain_db"]

    result = formulae.bode_f3db(freq_hz, gain_db)
    f3db = result["f_3db_hz"]

    assert f3db is not None, (
        f"bode_f3db returned None — gain never dropped 3 dB across {freq_hz[0]:.1f}"
        f"–{freq_hz[-1]:.1f} Hz; check wiring (W1->R->CH2 node, CH1@W1, C to GND)"
    )
    rel_err = abs(f3db - fc) / fc
    assert rel_err < 0.10, (
        f"f_3dB={f3db:.1f} Hz deviates {rel_err*100:.1f}% from expected {fc:.1f} Hz "
        f"(tolerance 10%); check R and C values, or update BODE_RC_FC_HZ"
    )
