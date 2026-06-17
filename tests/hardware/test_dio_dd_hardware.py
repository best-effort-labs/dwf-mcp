"""Hardware smoke test for DIO on the Digital Discovery (devid 4).

Wiring (DD left side connector): DIO24 (out) -> DIO25 (in) + JL GND -> DD GND.
Run: DWF_TEST_SERIAL=210321AD4ECF pytest tests/hardware/test_dio_dd_hardware.py -m hardware -v
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
@pytest.mark.device(devid=4)
@pytest.mark.jumperless(connections={
    "loopback": ("DIO24", "DIO25"),
    "gnd": ("DD_GND", "GND"),
})
def test_dd_dio_loopback_high_low(device, artifacts) -> None:
    from dwf_mcp.instruments.dio import DIO

    dio = DIO(device=device, artifacts=artifacts)

    dio.set_direction(pin="dio24", direction="out")
    dio.set_direction(pin="dio25", direction="in")

    dio.set(pin="dio24", state=1)
    result_high = dio.read(pin="dio25")
    assert result_high["state"] == 1, f"expected DIO25=1, got {result_high['state']}"

    dio.set(pin="dio24", state=0)
    result_low = dio.read(pin="dio25")
    assert result_low["state"] == 0, f"expected DIO25=0, got {result_low['state']}"
