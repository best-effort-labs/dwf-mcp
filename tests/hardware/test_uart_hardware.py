"""UART hardware smoke test. Requires TX(DIO0)→RX(DIO1) loopback."""
from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_uart_loopback(app) -> None:
    async def run() -> None:
        await app.call_tool("uart.configure", {
            "baud_rate": 9600, "tx_pin": "dio0", "rx_pin": "dio1",
        })
        await app.call_tool("uart.write", {"data": [0x48, 0x65, 0x6C, 0x6C, 0x6F]})
        time.sleep(0.05)
        result = await app.call_tool("uart.read", {"length": 5, "timeout_s": 1.0})
        assert result["data"] == [0x48, 0x65, 0x6C, 0x6C, 0x6F], f"got: {result['data']}"
        assert result["parity_error"] is False

    asyncio.run(run())
