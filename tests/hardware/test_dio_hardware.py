"""Hardware smoke test for DIO.

Wiring: DIO0 (out) → DIO1 (in) loopback.
Run: pytest tests/hardware/test_dio_hardware.py -m hardware -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_dio_loopback_high_low(tmp_path: Path) -> None:
    pytest.importorskip("pydwf")

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.dio import DIO
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    try:
        arts = ArtifactWriter(workspace=tmp_path)
        dio = DIO(device=device, artifacts=arts)

        dio.set_direction(pin="dio0", direction="out")
        dio.set_direction(pin="dio1", direction="in")

        dio.set(pin="dio0", state=1)
        result_high = dio.read(pin="dio1")
        assert result_high["state"] == 1, f"expected DIO1=1, got {result_high['state']}"

        dio.set(pin="dio0", state=0)
        result_low = dio.read(pin="dio1")
        assert result_low["state"] == 0, f"expected DIO1=0, got {result_low['state']}"
    finally:
        device.close()
