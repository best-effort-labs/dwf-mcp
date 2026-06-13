"""Hardware smoke test for DIO.

Wiring: DIO0 (out) → DIO1 (in) loopback.
Run: pytest tests/hardware/test_dio_hardware.py -m hardware -v
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_dio_loopback_high_low(device, artifacts) -> None:
    from dwf_mcp.instruments.dio import DIO

    dio = DIO(device=device, artifacts=artifacts)

    dio.set_direction(pin="dio0", direction="out")
    dio.set_direction(pin="dio1", direction="in")

    dio.set(pin="dio0", state=1)
    result_high = dio.read(pin="dio1")
    assert result_high["state"] == 1, f"expected DIO1=1, got {result_high['state']}"

    dio.set(pin="dio0", state=0)
    result_low = dio.read(pin="dio1")
    assert result_low["state"] == 0, f"expected DIO1=0, got {result_low['state']}"
