"""Hardware validation for the DigitalIn (logic) edge trigger — the path that
shipped broken (crash + detector never configured) because no hardware test
exercised it. Self-wires via the digital_loopback descriptor (AD3 dio0->dio1,
DD dio24->dio25, ADP2230 dio0->dio1): a Pattern clock on out_pin loops back to
in_pin, and the logic instrument triggers on a rising edge of in_pin.

Decisive by construction: auto-timeout is set to 0 (wait for a REAL trigger),
so a misconfigured detector cannot complete and capture() raises at its host
deadline. A working rising-edge detector completes, and the trigger lands at
the position computed from position_s.

Run: DWF_TEST_SERIAL=<serial> pytest tests/hardware/test_logic_trigger_hardware.py -m hardware -v
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"logic", "pattern"})
def test_logic_rising_edge_trigger_fires_and_positions(
    device, artifacts, digital_loopback
) -> None:
    from dwf_mcp.instruments.logic import Logic
    from dwf_mcp.instruments.pattern import Pattern

    out_pin, in_pin = digital_loopback
    pat = Pattern(device=device, artifacts=artifacts)
    logic = Logic(device=device, artifacts=artifacts)

    sample_rate_hz = 1_000_000.0
    buffer_size = 4096
    samples_after = buffer_size // 4          # 1024 post-trigger samples
    position_s = samples_after / sample_rate_hz
    expected_trig_index = buffer_size - samples_after  # 3072

    # 10 kHz clock at 1 MHz sampling => a clean square wave on the input pin.
    # (1 kHz would overflow the pattern generator's counter at divider=1 and
    # produce no output.) With timeout_s=0 below, the capture can ONLY complete
    # via a real rising-edge trigger, which always lands at the configured
    # position -- so the edge must appear within a few samples of expected_trig_index.
    pat.configure(pin=out_pin, function="Clock", frequency_hz=10_000.0,
                  duty=0.5, idle_state="low")
    try:
        pat.start(pin=out_pin)
        logic.configure(pins=[in_pin], sample_rate_hz=sample_rate_hz, buffer_size=buffer_size)
        # timeout_s=0 -> no auto-trigger; only a real rising edge completes the capture.
        logic.set_trigger(source="detector_digital_in", pin=in_pin,
                          condition="Rising", position_s=position_s, timeout_s=0.0)
        result = logic.capture()
        data = np.load(result["path"])[in_pin].astype(int)
    finally:
        pat.stop(pin=out_pin)

    assert 0 in data and 1 in data, "expected clock transitions on the input pin"

    # A rising edge (0 -> 1) must sit at the trigger position.
    diffs = np.diff(data)
    rising_indices = np.where(diffs == 1)[0] + 1  # index of the sample that is now 1
    assert rising_indices.size > 0, "no rising edges captured at all"
    nearest = int(rising_indices[np.argmin(np.abs(rising_indices - expected_trig_index))])
    assert abs(nearest - expected_trig_index) <= 5, (
        f"rising edge at {nearest}, expected trigger near {expected_trig_index} "
        f"(position_s={position_s}); detector/position mis-set"
    )
