"""SPI hardware smoke test. Requires MOSI(DIO1)→MISO(DIO2) loopback."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO1", "DIO2")})
def test_spi_loopback_transfer(app) -> None:
    async def run() -> None:
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "frequency_hz": 1_000_000, "mode": 0,
            "mosi_pin": "dio1", "miso_pin": "dio2", "cs_pin": "dio3",
        })
        result = await app.call_tool("spi.transfer", {"data": [0xA5, 0x5A, 0xFF, 0x00]})
        assert result["sent"] == [0xA5, 0x5A, 0xFF, 0x00]
        assert result["received"] == result["sent"]

    asyncio.run(run())
