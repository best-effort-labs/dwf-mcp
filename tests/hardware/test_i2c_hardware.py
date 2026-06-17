"""Hardware smoke for i2c. Requires AD3 with bench pull-ups on the configured DIO pins.

Even without any I2C slave on the bus, the scan should run cleanly and return [] —
proving the wire toggled and the protocol class came up.
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"i2c"})
@pytest.mark.jumperless(connections={
    "sda_pwr": ("TOP_RAIL", "I2C_SDA_R_A"),
    "sda_sig": ("DIO0", "I2C_SDA_R_B"),
    "scl_pwr": ("TOP_RAIL", "I2C_SCL_R_A"),
    "scl_sig": ("DIO1", "I2C_SCL_R_B"),
})
def test_i2c_scan_runs_without_error(device, artifacts) -> None:
    from dwf_mcp.instruments.i2c import I2C

    i2c = I2C(device=device, artifacts=artifacts)
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.scan()
    assert "found" in result
    assert isinstance(result["found"], list)
    # Empty list is OK (no slaves); the point is the protocol class ran.
