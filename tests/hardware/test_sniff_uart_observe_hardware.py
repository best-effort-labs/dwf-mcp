"""UART observe-mode sniff hardware test — RP2350B drives UART0,
the AD3 ``sniff.uart_start/stop`` tools capture via DigitalIn observe-mode.

Same RP2350B stimulus as ``test_sniff_uart_hardware.py``; the difference is
that the new async tools return immediately and the test stays in a single
asyncio event loop (rather than running the blocking ``sniff.uart`` on a
worker thread).

Wiring (via Jumperless, automatic):
  AD3_GND       → GND  (shared reference for digital signals)
  UART_TX node  → DIO0 (RP2350B UART0 TX → AD3 sniff RX)

Run:
  pytest tests/hardware/test_sniff_uart_observe_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow.parquet as pq
import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.device_config("max_digital_in")]


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("sniff_uart_observe")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    result = asyncio.run(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.run(app.call_tool("waveforms.close", {}))


_RP2350B_UART_WRITE = """
from machine import UART
import time
u = UART(0, 9600)
u.init(9600, 8, None, 1)
u.write(b'Hello observe!')
time.sleep_ms(50)
"""


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={
    "gnd_bridge":  ("AD3_GND", "GND"),
    "tx_to_dio0":  ("UART_TX", "DIO0"),
})
async def test_sniff_uart_observe_captures_rp2350b_message(
    app, jumperless, tmp_path: Path,
) -> None:
    """sniff.uart_start + RP2350B UART write + sniff.uart_stop decodes payload."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    start_result = await app.call_tool("sniff.uart_start", {
        "rx_pin": "dio0",
        "baud": 9600,
        "max_duration_s": 2.0,
        "polarity": 0,           # TTL (idle HIGH) — RP2350B is 3.3V CMOS
    })
    sniff_id = start_result["sniff_id"]

    # Let the record_loop spin up before stimulus.
    await asyncio.sleep(0.2)

    # Fire stimulus on a worker thread so the event loop keeps polling DigitalIn.
    await asyncio.to_thread(jumperless.exec, _RP2350B_UART_WRITE)

    # Allow trailing samples to drain.
    await asyncio.sleep(0.1)

    stop_result = await app.call_tool("sniff.uart_stop", {"sniff_id": sniff_id})

    assert stop_result["artifact_error"] is None, (
        f"artifact_error: {stop_result['artifact_error']}"
    )
    assert stop_result["artifact_path"] is not None
    assert stop_result["count"] > 0, "no UART frames captured"
    assert stop_result["error_count"] == 0, (
        f"{stop_result['error_count']} parity errors"
    )

    table = pq.read_table(stop_result["artifact_path"])
    payload = b"".join(row.as_py() for row in table.column("data"))
    assert b"Hello observe!" in payload, (
        f"expected 'Hello observe!' in {payload!r}"
    )
