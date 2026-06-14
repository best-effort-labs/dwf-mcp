"""Hardware smoke test for scope.

Requires AD3 with W1 wired to scope ch1+ (or via signal generator).

Run: pytest tests/hardware/test_scope_hardware.py -m hardware -v
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"awg_to_scope": ("W1", "CH1_POS")})
def test_scope_captures_1khz_sine_from_awg(device, artifacts) -> None:
    """Start AWG ch1 at 1 kHz sine, capture on scope ch1, assert freq estimate near 1 kHz.

    Requires: W1 wired to scope ch1+ (or AD3 internal loopback if available).
    """
    from pydwf import DwfAnalogOutFunction, DwfAnalogOutNode  # type: ignore[import-untyped]

    from dwf_mcp.instruments.scope import Scope

    backend = device.backend

    # Drive AWG ch1 = 1 kHz sine, 1 Vpp, via raw pydwf (AWG instrument not yet wired).
    ao = backend._device.analogOut  # type: ignore[attr-defined]
    ao.nodeEnableSet(0, DwfAnalogOutNode.Carrier, True)
    ao.nodeFunctionSet(0, DwfAnalogOutNode.Carrier, DwfAnalogOutFunction.Sine)
    ao.nodeFrequencySet(0, DwfAnalogOutNode.Carrier, 1000.0)
    ao.nodeAmplitudeSet(0, DwfAnalogOutNode.Carrier, 1.0)
    ao.configure(0, True)

    scope = Scope(device=device, artifacts=artifacts)
    scope.configure(
        channels=[1],
        range_v=5.0,
        offset_v=0.0,
        coupling="DC",
        sample_rate_hz=100_000,
        buffer_size=4096,
    )
    scope.set_trigger(
        source="detector_analog_in",
        channel=1,
        level_v=0.0,
        condition="Rising",
        position_s=0.0,
        timeout_s=1.0,
    )
    result = scope.capture()
    freq = result["summary"]["ch1"]["freq_estimate"]
    assert 900 < freq < 1100, f"expected ~1000 Hz, got {freq}"
