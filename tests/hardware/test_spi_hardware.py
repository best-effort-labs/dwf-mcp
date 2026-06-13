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


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO1", "DIO2")})
def test_spi_transfer_without_cs_still_echoes(app) -> None:
    """assert_cs=False must keep the bus in MOSI/MISO mode (transfer_type 1).
    The old backend passed transfer_type=0 (SISO) here, breaking the echo."""
    async def run() -> None:
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "frequency_hz": 1_000_000, "mode": 0,
            "mosi_pin": "dio1", "miso_pin": "dio2",
        })
        result = await app.call_tool(
            "spi.transfer", {"data": [0xDE, 0xAD, 0xBE, 0xEF], "assert_cs": False}
        )
        assert result["received"] == [0xDE, 0xAD, 0xBE, 0xEF]

    asyncio.run(run())


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO1", "DIO2")})
def test_spi_write_and_read_paths_execute(app) -> None:
    """write() and read() were previously broken (write crammed len*8 into
    bits_per_word; read omitted the word-count arg entirely). Drive both against
    real hardware with a CS pin configured to confirm they complete and return
    well-formed results — a multi-byte write would have raised on the old
    bits_per_word>32 path."""
    async def run() -> None:
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "frequency_hz": 1_000_000, "mode": 0,
            "mosi_pin": "dio1", "miso_pin": "dio2", "cs_pin": "dio3",
        })
        w = await app.call_tool("spi.write", {"data": [0x11, 0x22, 0x33, 0x44, 0x55]})
        assert w["bytes_written"] == 5
        r = await app.call_tool("spi.read", {"length": 3})
        assert len(r["data"]) == 3

    asyncio.run(run())
