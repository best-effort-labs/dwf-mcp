"""CAN hardware smoke test. Requires TX(DIO0)→RX(DIO1) loopback at 125kbps."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_can_send_receive_loopback(app) -> None:
    async def run() -> None:
        await app.call_tool("can.configure", {
            "tx_pin": "dio0", "rx_pin": "dio1", "bit_rate": 125_000,
        })
        await app.call_tool("can.send", {"id": 0x123, "data": [0x01, 0x02, 0x03]})
        result = await app.call_tool("can.receive", {"timeout_s": 1.0})
        assert result["id"] == 0x123, f"expected 0x123, got {result['id']}"
        assert result["data"] == [0x01, 0x02, 0x03]
        assert result["extended"] is False

    asyncio.run(run())
