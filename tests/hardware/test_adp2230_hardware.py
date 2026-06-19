"""Standalone ADP2230 hardware tests — no external wiring required.

Run: DWF_TEST_SERIAL=210417BAF36D .venv/bin/pytest tests/hardware/test_adp2230_hardware.py -m hardware -v

These verify capabilities, electrical-config round-trips, supply, and AWG/pattern
start/stop using only the device itself. Physical signal verification (loopback,
scope-reads-AWG) lives in the wired suite (Task 8), gated on a Jumperless.
"""
from __future__ import annotations

import time

import pytest


def _is_adp2230(device) -> bool:
    return device.profile is not None and device.profile.devid == 14


@pytest.mark.hardware
@pytest.mark.requires(instruments={"dio"})
def test_adp2230_device_caps(device) -> None:
    if not _is_adp2230(device):
        pytest.skip("DUT is not an ADP2230 (set DWF_TEST_SERIAL=210417BAF36D)")
    info = device._info
    assert info is not None
    assert info.analog_in_channels == 2
    assert device.profile.user_awg_count == 1          # ONE user AWG
    assert device.inventory.awg_pins == ["awg1"]
    assert info.dio_count == 16
    assert info.dio_drive_supported is True
    assert info.dio_pull_supported is True
    # Drive range 4..16 mA (datasheet) — allow float tolerance.
    assert abs(info.dio_drive_amp_min - 0.004) < 1e-6
    assert abs(info.dio_drive_amp_max - 0.016) < 1e-6


@pytest.mark.hardware
@pytest.mark.requires(instruments={"dio"})
def test_adp2230_drive_and_pull_set(device, artifacts) -> None:
    """set_drive / set_pull are internal config — no wiring needed to confirm the
    SDK accepts them. Physical effect on a pin is verified in the wired suite."""
    if not _is_adp2230(device):
        pytest.skip("DUT is not an ADP2230")
    from dwf_mcp.instruments.dio import DIO

    dio = DIO(device=device, artifacts=artifacts)
    out = dio.set_drive(milliamps=8.0, slew=0)
    assert out["milliamps"] == 8.0
    for mode in ("up", "down", "keeper", "none"):
        res = dio.set_pull(pin="dio0", mode=mode)
        assert res["mode"] == mode


@pytest.mark.hardware
@pytest.mark.requires(instruments={"supply"})
def test_adp2230_supply_vpos_round_trip(device, artifacts) -> None:
    """Programmable V+ to 1.0 V, read back, disable. No external load required.
    Energizes a real rail — must disable in teardown (the finally block)."""
    if not _is_adp2230(device):
        pytest.skip("DUT is not an ADP2230")
    from dwf_mcp.instruments.supply import Supply

    supply = Supply(device=device, artifacts=artifacts)
    try:
        supply.set(channel="vpos", voltage=1.0, current_limit=0.1)
        supply.enable(channel="vpos")
        # Poll until V+ settles within [0.7, 1.3] V — handles residual capacitive
        # charge from a prior high-setpoint run that makes a fixed sleep flaky.
        _deadline = time.monotonic() + 5.0
        state = None
        while True:
            state = supply.read(channel="vpos")
            if state["enabled"] and 0.7 < state["measured"]["voltage"] < 1.3:
                break
            if time.monotonic() >= _deadline:
                pytest.fail(f"V+ did not settle to [0.7, 1.3] V within 5 s; last state: {state}")
            time.sleep(0.25)
        assert state["enabled"] is True, state
    finally:
        supply.disable(channel="vpos")
    state = supply.read(channel="vpos")
    assert state["enabled"] is False, state


@pytest.mark.hardware
@pytest.mark.requires(instruments={"awg"})
def test_adp2230_awg_start_stop(device, artifacts) -> None:
    if not _is_adp2230(device):
        pytest.skip("DUT is not an ADP2230")
    from dwf_mcp.instruments.awg import AWG

    awg = AWG(device=device, artifacts=artifacts)
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.start(channel=1)
    awg.stop(channel=1)
    # channel 2 must be rejected (single-AWG device)
    with pytest.raises(ValueError, match="out of range 1..1"):
        awg.configure(channel=2, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)


@pytest.mark.hardware
@pytest.mark.requires(instruments={"pattern"})
def test_adp2230_pattern_start_stop(device, artifacts) -> None:
    if not _is_adp2230(device):
        pytest.skip("DUT is not an ADP2230")
    from dwf_mcp.instruments.pattern import Pattern

    pat = Pattern(device=device, artifacts=artifacts)
    pat.configure(pin="dio0", function="Clock", frequency_hz=1000.0, duty=0.5,
                  idle_state="low")
    pat.start(pin="dio0")
    pat.stop(pin="dio0")
