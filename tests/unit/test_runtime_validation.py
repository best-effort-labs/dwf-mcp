from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.instruments.dio import DIO
from dwf_mcp.policy import SafetyPolicy


def _dio(tmp_path: Path) -> DIO:
    dev = DwfDevice(backend=FakeBackend(), policy=SafetyPolicy(),
                    allocator=PinAllocator(), workspace=tmp_path, idle_timeout_s=60)
    dev.open()
    return DIO(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))


def test_dio_set_rejects_pin_beyond_device(tmp_path: Path) -> None:
    dio = _dio(tmp_path)
    dio.set_direction(pin="dio16", direction="out")  # naming ok at schema level
    with pytest.raises(ValueError, match="not available"):
        dio.set(pin="dio16", state=1)


def test_list_pins_reports_live_inventory(tmp_path: Path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))

    async def run():
        await app.call_tool("waveforms.open", {})
        return await app.call_tool("waveforms.list_pins", {})
    result = asyncio.run(run())
    assert "dio15" in result["all_pins"]
    assert result["limits"]["sample_rate_max_hz"] == 100_000_000.0
