"""Portable wired hardware test that DIO pull config physically moves a pin.

Uses the digital_loopback fixture (out<->in DIO pair + GND). Runs on any device
whose profile reports pull support and that has a digital_loopback descriptor
(DD, ADP2230; AD3 has no pull → skipped). Complements test_adp2230_hardware.py's
set-path check, which only confirms set_pull doesn't raise.

NOTE: a pull-up-then-pull-down per-pin test is intentionally omitted — on the
ADP2230 the DIO pull is BANK-GLOBAL and the per-bit read-modify-write in
dio_pull_set can't switch/clear pull per-pin (see
docs/tool-bugs/2026-06-18-dwf-mcp-adp2230-bank-global-pull.md). Keeper below sets
the whole bank to one mode, so it validates the physical pull path cleanly.

Run: DWF_TEST_SERIAL=<serial> PYTHONPATH=~/work/jlv5-harness/src \\
     .venv/bin/pytest tests/hardware/test_dio_pull_hardware.py -m hardware -v
"""
from __future__ import annotations

import time

import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"dio"})
def test_dio_keeper_holds_last_driven(device, artifacts, digital_loopback) -> None:
    """Keeper (bus-hold): after a pin is driven then released, the net holds the last
    driven level. Asserting BOTH directions (held-high after a high drive AND held-low
    after a low drive) proves a true latch — a merely-floating net rests at one level
    and can't hold both. This also exercises the physical pull path end-to-end."""
    info = device._info
    if not (info and info.dio_pull_supported):
        pytest.skip("device profile does not support DIO pull config")
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
