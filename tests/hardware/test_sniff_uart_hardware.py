"""UART sniff hardware test — stimulus from RP2350B onboard the Jumperless.

Approach follows the official ``scripts/ex/uart_loopback.py`` example: instantiate
``machine.UART(0, baudrate)`` with NO pin override. The Jumperless firmware has
already wired the routable ``UART_TX`` / ``UART_RX`` nodes to the chip pins
``UART(0)`` uses by default. We route ``UART_TX → DIO0`` via the crossbar and
sniff there.

Wiring (via Jumperless, automatic):
  AD3_GND       → GND  (shared reference for digital signals)
  UART_TX node  → DIO0 (RP2350B UART0 TX → AD3 sniff RX)

The RP2350B drives 3.3V TTL (idle HIGH, start LOW). Empirically the AD3's
default ``polarity=0`` matches this convention (verified against the existing
``test_uart_hardware.py`` AD3-internal loopback, which is also TTL).

Run:
  pytest tests/hardware/test_sniff_uart_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tests.hardware.conftest import wait_for_sniff_claim

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("sniff_uart")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    result = asyncio.run(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.run(app.call_tool("waveforms.close", {}))


@pytest.mark.jumperless(connections={
    "gnd_bridge":  ("AD3_GND", "GND"),
    "tx_to_dio0":  ("UART_TX", "DIO0"),
})
def test_sniff_uart_rp2350b_short_message(app, jumperless, tmp_path: Path) -> None:
    """RP2350B sends 'Hello AD3!' via UART(0, 9600); sniff captures it."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.uart", {
            "rx_pin": "dio0",
            "baud": 9600,
            "duration_s": 1.0,
            "polarity": 0,           # TTL (idle HIGH) — RP2350B is 3.3V CMOS
            "poll_interval_s": 0.005,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()
    wait_for_sniff_claim(app, "sniff_uart")

    jumperless.exec("""
from machine import UART
import time
u = UART(0, 9600)
u.init(9600, 8, None, 1)
u.write(b'Hello AD3!')
time.sleep_ms(50)
""")

    t.join(timeout=2.0)
    assert not t.is_alive(), "sniff.uart timed out"

    r = result["sniff"]
    assert r["artifact_error"] is None, f"artifact_error: {r['artifact_error']}"
    assert r["count"] > 0, "no UART frames captured"
    assert r["error_count"] == 0, f"{r['error_count']} parity errors"

    table = pq.read_table(r["artifact_path"])
    payload = b"".join(row.as_py() for row in table.column("data"))
    assert b"Hello AD3!" in payload, f"expected 'Hello AD3!' in {payload!r}"


@pytest.mark.jumperless(connections={
    "gnd_bridge":  ("AD3_GND", "GND"),
    "tx_to_dio0":  ("UART_TX", "DIO0"),
})
def test_sniff_uart_rp2350b_higher_baud(app, jumperless, tmp_path: Path) -> None:
    """115200 baud — verifies sniff works at the loopback example's rate."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.uart", {
            "rx_pin": "dio0",
            "baud": 115200,
            "duration_s": 0.5,
            "polarity": 0,
            "poll_interval_s": 0.002,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()
    wait_for_sniff_claim(app, "sniff_uart")

    jumperless.exec("""
from machine import UART
import time
u = UART(0, 115200)
u.init(115200, 8, None, 1)
u.write(b'ABCDEFGHIJ')
time.sleep_ms(10)
""")

    t.join(timeout=2.0)
    assert not t.is_alive()
    r = result["sniff"]
    assert r["count"] > 0, "no frames at 115200"
    assert r["error_count"] == 0
    table = pq.read_table(r["artifact_path"])
    payload = b"".join(row.as_py() for row in table.column("data"))
    assert b"ABCDEFGHIJ" in payload, f"got {payload!r}"
