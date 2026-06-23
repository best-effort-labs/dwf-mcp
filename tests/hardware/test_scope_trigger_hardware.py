"""Hardware verification that the scope (AnalogIn) trigger actually FIRES on the
configured edge/level and positions the capture — the existing scope test sets a
trigger but with timeout_s=1.0, so an auto-trigger would mask a broken detector
(the same blind spot that hid the logic-trigger bug).

Decisive by construction: timeout_s=0 disables the auto-trigger, so the capture can
only complete via a real rising-through-0V edge; position_s=0 puts that edge at the
buffer centre. Self-wires W1->CH1 via the analog_loopback fixture (Jumperless on AD3,
manual BNC on ADP2230).

Run: DWF_TEST_SERIAL=<serial> pytest tests/hardware/test_scope_trigger_hardware.py -m hardware -v
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"awg", "scope"})
def test_scope_trigger_fires_on_rising_zero_crossing(
    device, artifacts, analog_loopback
) -> None:
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.scope import Scope

    awg_ch, scope_ch = analog_loopback
    awg = AWG(device=device, artifacts=artifacts)
    scope = Scope(device=device, artifacts=artifacts)

    awg.configure(channel=awg_ch, function="Sine", frequency_hz=200.0, amplitude_v=1.0)
    try:
        awg.start(channel=awg_ch)
        scope.configure(channels=[scope_ch], range_v=5.0, offset_v=0.0, coupling="DC",
                        sample_rate_hz=100_000, buffer_size=4096)
        # timeout_s=0 -> no auto-trigger: only a real rising 0V crossing completes it.
        scope.set_trigger(source="detector_analog_in", channel=scope_ch, level_v=0.0,
                          condition="Rising", position_s=0.0, timeout_s=0.0)
        result = scope.capture()
        data = np.load(result["path"])[f"ch{scope_ch}"]
    finally:
        awg.stop(channel=awg_ch)

    center = len(data) // 2
    # Rising zero-crossings: sample <= 0 followed by sample > 0.
    rising_zc = np.where((data[:-1] <= 0) & (data[1:] > 0))[0]
    assert rising_zc.size > 0, "no rising zero-crossings captured"
    nearest = int(rising_zc[np.argmin(np.abs(rising_zc - center))])
    assert abs(nearest - center) <= 15, (
        f"rising 0V crossing at {nearest}, expected at buffer centre {center} "
        f"(position_s=0); trigger detector/position mis-set"
    )
