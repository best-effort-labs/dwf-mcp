"""SPI hardware smoke test. Requires MOSI(DIO1)→MISO(DIO2) loopback."""
from __future__ import annotations

import asyncio
import pytest

from dwf_mcp.server import build_app


@pytest.mark.hardware
def test_spi_loopback_transfer() -> None:
    app = build_app(backend_name="pydwf")

    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "frequency_hz": 1_000_000, "mode": 0,
            "mosi_pin": "dio1", "miso_pin": "dio2", "cs_pin": "dio3",
        })
        result = await app.call_tool("spi.transfer", {"data": [0xA5, 0x5A, 0xFF, 0x00]})
        assert result["sent"] == [0xA5, 0x5A, 0xFF, 0x00]
        assert result["received"] == result["sent"]
        await app.call_tool("waveforms.close", {})

    asyncio.run(run())
