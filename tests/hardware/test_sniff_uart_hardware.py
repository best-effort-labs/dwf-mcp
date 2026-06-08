"""UART sniff hardware test — stimulus from RP2350B onboard the Jumperless.

Wiring (via Jumperless, automatic):
  UART_TX (RP2350B GP0) → DIO0 (AD3)

The RP2350B transmits bytes with machine.UART(0, baudrate=9600, tx=Pin(0)).
sniff.uart captures on DIO0 RX-only.

Because sniff.uart blocks for duration_s, we start it in a thread and fire
the RP2350B transmission from the main thread 200 ms later (after UART engine
set-up completes).

Run:
  pytest tests/hardware/test_sniff_uart_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

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
    # RP2350B UART_TX (GP0) → AD3 DIO0 (sniff RX)
    "rp_tx_to_ad3": ("UART_TX", "DIO0"),
})
def test_sniff_uart_rp2350b_stimulus(app, jumperless, tmp_path: Path) -> None:
    """RP2350B sends 3 known bytes; sniff.uart captures them on DIO0."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.uart", {
            "rx_pin": "dio0",
            "baud": 9600,
            "duration_s": 1.0,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()

    # uart_sniff does: reset → rateSet → rxSet → rx(0) → rx(1) → sleep(10ms) → poll loop
    # 200 ms is plenty for the UART engine to be ready.
    time.sleep(0.2)

    jumperless.exec("""
from machine import UART, Pin
u = UART(0, baudrate=9600, tx=Pin(0))
u.write(b'\\x41\\x42\\x43')
""")

    t.join(timeout=2.0)
    assert not t.is_alive(), "sniff.uart timed out"

    r = result["sniff"]
    assert r["artifact_error"] is None, f"artifact_error: {r['artifact_error']}"
    assert r["count"] > 0, "no UART frames captured"
    assert r["error_count"] == 0, f"unexpected parity errors: {r['error_count']}"

    import pyarrow.parquet as pq
    table = pq.read_table(r["artifact_path"])
    all_bytes = b"".join(row.as_py() for row in table.column("data"))
    assert b"\x41\x42\x43" in all_bytes, (
        f"expected 0x41 0x42 0x43 in capture, got: {all_bytes.hex()}"
    )


@pytest.mark.jumperless(connections={
    "rp_tx_to_ad3": ("UART_TX", "DIO0"),
})
def test_sniff_uart_parity_errors_absent(app, jumperless, tmp_path: Path) -> None:
    """Verify clean 8N1 transmission at 115200 produces no parity errors."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.uart", {
            "rx_pin": "dio0",
            "baud": 115200,
            "duration_s": 1.0,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()
    time.sleep(0.2)

    jumperless.exec("""
from machine import UART, Pin
u = UART(0, baudrate=115200, tx=Pin(0))
u.write(b'Hello World')
""")

    t.join(timeout=2.0)
    assert not t.is_alive()

    r = result["sniff"]
    assert r["error_count"] == 0
    assert r["count"] > 0
