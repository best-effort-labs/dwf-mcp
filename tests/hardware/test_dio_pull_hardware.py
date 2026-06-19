"""Portable wired hardware test that DIO pull config physically moves a pin.

Uses the digital_loopback fixture (out<->in DIO pair + GND). Runs on any device
whose profile reports pull support and that has a digital_loopback descriptor
(DD, ADP2230; AD3 has no pull → skipped). Complements test_adp2230_hardware.py's
set-path check, which only confirms set_pull doesn't raise.

Works on both pull granularities: per-pin (DD) and bank-global (ADP2230, where
set_pull moves the whole bank together — see dio_pull_set). Verified on the DD
(per-pin) this session; the bank-global path is covered by unit tests + the prior
ADP2230 empirical confirmation.

Run: DWF_TEST_SERIAL=<serial> PYTHONPATH=~/work/jlv5-harness/src \\
     .venv/bin/pytest tests/hardware/test_dio_pull_hardware.py -m hardware -v
"""
from __future__ import annotations

import time

import pytest


def _skip_if_no_pull(device) -> None:
    info = device._info
    if not (info and info.dio_pull_supported):
        pytest.skip("device profile does not support DIO pull config")


@pytest.mark.hardware
@pytest.mark.requires(instruments={"dio"})
def test_dio_pull_up_down_moves_pin(device, artifacts, digital_loopback) -> None:
    """With the line undriven, pull-up reads high and pull-down reads low — i.e. the
    pull actually drives the net, and switching modes clears the previous pull."""
    _skip_if_no_pull(device)
    from dwf_mcp.instruments.dio import DIO

    out_pin, in_pin = digital_loopback
    dio = DIO(device=device, artifacts=artifacts)
    dio.set_direction(pin=in_pin, direction="in")
    dio.set_direction(pin=out_pin, direction="in")  # release the driver -> net held only by pull
    try:
        dio.set_pull(pin=in_pin, mode="up")
        time.sleep(0.3)
        assert dio.read(pin=in_pin)["state"] == 1, "pull-up did not pull the net high"

        dio.set_pull(pin=in_pin, mode="down")
        time.sleep(0.3)
        assert dio.read(pin=in_pin)["state"] == 0, "pull-down did not pull the net low"
    finally:
        dio.set_pull(pin=in_pin, mode="none")


@pytest.mark.hardware
@pytest.mark.requires(instruments={"dio"})
def test_dio_keeper_holds_last_driven(device, artifacts, digital_loopback) -> None:
    """Keeper (bus-hold): after a pin is driven then released, the net holds the last
    driven level. Asserting BOTH directions (held-high after a high drive AND held-low
    after a low drive) proves a true latch — a merely-floating net rests at one level
    and can't hold both. This also exercises the physical pull path end-to-end."""
    _skip_if_no_pull(device)
    from dwf_mcp.instruments.dio import DIO

    out_pin, in_pin = digital_loopback
    dio = DIO(device=device, artifacts=artifacts)
    dio.set_direction(pin=in_pin, direction="in")

    def drive_then_release(state: int) -> int:
        dio.set_direction(pin=out_pin, direction="out")
        dio.set(pin=out_pin, state=state)
        time.sleep(0.05)
        dio.set_direction(pin=out_pin, direction="in")  # high-Z; keeper must hold
        time.sleep(1.5)
        return dio.read(pin=in_pin)["state"]

    try:
        dio.set_pull(pin=in_pin, mode="keeper")
        assert drive_then_release(1) == 1, "keeper did not hold the last-driven HIGH"
        assert drive_then_release(0) == 0, "keeper did not hold the last-driven LOW"
    finally:
        dio.set_pull(pin=in_pin, mode="none")
