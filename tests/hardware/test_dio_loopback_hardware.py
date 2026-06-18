"""Portable hardware smoke test for DIO: out pin -> in pin high/low loopback on any
device with a digital_loopback descriptor (AD3 dio0->dio1, DD dio24->dio25, ADP2230 dio0->dio1).

Run: DWF_TEST_SERIAL=<serial> pytest tests/hardware/test_dio_loopback_hardware.py -m hardware -v
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"dio"})
def test_dio_loopback_high_low(device, artifacts, digital_loopback) -> None:
    from dwf_mcp.instruments.dio import DIO

    out_pin, in_pin = digital_loopback
    dio = DIO(device=device, artifacts=artifacts)

    dio.set_direction(pin=out_pin, direction="out")
    dio.set_direction(pin=in_pin, direction="in")

    dio.set(pin=out_pin, state=1)
    hi = dio.read(pin=in_pin)
    assert hi["state"] == 1, f"expected {in_pin}=1, got {hi['state']}"

    dio.set(pin=out_pin, state=0)
    lo = dio.read(pin=in_pin)
    assert lo["state"] == 0, f"expected {in_pin}=0, got {lo['state']}"
