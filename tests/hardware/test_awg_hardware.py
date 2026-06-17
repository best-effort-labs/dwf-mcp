"""Hardware smoke test for AWG.

Wiring: W1 → scope ch1+ (same wire as existing scope hardware test).
Run: pytest tests/hardware/test_awg_hardware.py -m hardware -v
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"awg", "scope"})
@pytest.mark.jumperless(connections={"awg_to_scope": ("W1", "CH1_POS")})
def test_awg_sine_captured_by_scope(device, artifacts) -> None:
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.scope import Scope

    awg = AWG(device=device, artifacts=artifacts)
    scope = Scope(device=device, artifacts=artifacts)

    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.start(channel=1)

    scope.configure(channels=[1], range_v=5.0, sample_rate_hz=100_000, buffer_size=4096)
    scope.set_trigger(
        source="detector_analog_in", channel=1, level_v=0.0,
        condition="Rising", timeout_s=2.0,
    )
    result = scope.capture()
    freq = result["summary"]["ch1"]["freq_estimate"]
    assert 900 < freq < 1100, f"expected ~1000 Hz, got {freq}"
