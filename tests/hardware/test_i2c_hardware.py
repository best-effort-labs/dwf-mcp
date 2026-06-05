"""Hardware smoke for i2c. Requires AD3 with bench pull-ups on the configured DIO pins.

Even without any I2C slave on the bus, the scan should run cleanly and return [] —
proving the wire toggled and the protocol class came up.
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={
    "sda_pwr": ("TOP_RAIL", "I2C_SDA_R_A"),
    "sda_sig": ("DIO0", "I2C_SDA_R_B"),
    "scl_pwr": ("TOP_RAIL", "I2C_SCL_R_A"),
    "scl_sig": ("DIO1", "I2C_SCL_R_B"),
})
def test_i2c_scan_runs_without_error(tmp_path) -> None:
    pytest.importorskip("pydwf")
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.i2c import I2C
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend, policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path, idle_timeout_s=60,
    )
    device.open()
    try:
        i2c = I2C(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
        i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
        result = i2c.scan()
        assert "found" in result
        assert isinstance(result["found"], list)
        # Empty list is OK (no slaves); the point is the protocol class ran.
    finally:
        device.close()
