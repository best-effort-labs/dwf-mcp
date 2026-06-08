"""CAN hardware smoke test.

The AD3 Protocol CAN requires an external CAN transceiver and a second CAN node
to complete the ACK handshake; direct GPIO loopback does not decode frames.

This test verifies the TX path only: configure CAN, send a frame, and capture
the DIO0 output with DigitalIn to confirm a frame was transmitted (> 10 transitions).
"""
from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.hardware
def test_can_tx_frame_activity(app) -> None:
    """CAN TX path smoke test: verify a frame is generated on DIO0."""
    from pydwf import DwfState, DwfAcquisitionMode

    async def configure_can() -> None:
        await app.call_tool("can.configure", {
            "tx_pin": "dio0", "rx_pin": "dio1", "bit_rate": 10_000,
        })

    asyncio.run(configure_can())

    backend = app.device.backend
    device = backend._device
    can = device.protocol.can
    din = device.digitalIn

    # Set up DigitalIn to capture DIO0 at 1MHz for 4096 samples
    din.reset()
    din.inputOrderSet(False)
    clk = din.internalClockInfo()
    divider = max(1, round(clk / 1_000_000))
    din.dividerSet(divider)
    din.bufferSizeSet(4096)
    din.acquisitionModeSet(DwfAcquisitionMode.Single)
    din.configure(False, True)

    # Send a CAN frame
    can.tx(0x123, False, False, bytes([0x01, 0x02, 0x03]))

    # Wait for capture to complete (frame at 10kbps takes ~7ms)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        state = din.status(True)
        if state == DwfState.Done:
            break
        time.sleep(0.001)

    raw = din.statusData(4096, 16)
    dio0 = [(int(s) >> 0) & 1 for s in raw]
    transitions = sum(1 for i in range(1, len(dio0)) if dio0[i] != dio0[i - 1])

    din.reset()

    assert transitions >= 10, (
        f"Expected ≥10 transitions on DIO0 (CAN frame activity), got {transitions}. "
        "Check that CAN TX is wired to DIO0."
    )
