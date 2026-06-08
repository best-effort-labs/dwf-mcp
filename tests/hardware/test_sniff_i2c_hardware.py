"""I2C sniff hardware test — stimulus from RP2350B onboard the Jumperless.

Wiring (via Jumperless, automatic):
  GPIO_1 (RP2350B GP20, SDA) → DIO0 (AD3 SDA sniff pin)
  GPIO_2 (RP2350B GP21, SCL) → DIO1 (AD3 SCL sniff pin)
  I2C_SDA_R_A (28) ↔ I2C_SDA_R_B (58): pre-placed 4.7kΩ SDA pull-up
  I2C_SCL_R_A (29) ↔ I2C_SCL_R_B (59): pre-placed 4.7kΩ SCL pull-up

The RP2350B acts as I2C master on machine.I2C(0, sda=Pin(20), scl=Pin(21)).
sniff.i2c captures transactions on DIO0/DIO1.

No peripheral at 0x50 — the RP2350B gets NACK after the address byte, but the
AD3 spy still captures the full transaction (START + addr + NACK + STOP).

Same threading approach as test_sniff_uart_hardware.py — start sniff in a thread,
fire RP2350B I2C master from main thread 200 ms later.

Run:
  pytest tests/hardware/test_sniff_i2c_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
import pyarrow.parquet as pq

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("sniff_i2c")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    result = asyncio.run(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.run(app.call_tool("waveforms.close", {}))


@pytest.mark.jumperless(connections={
    # AD3_GND must share reference with Jumperless GND for digital signals to decode
    "gnd_bridge": ("AD3_GND", "GND"),
    # RP2350B I2C master pins → AD3 sniff pins
    "rp_sda_to_ad3": ("GPIO_1", "DIO0"),
    "rp_scl_to_ad3": ("GPIO_2", "DIO1"),
    # Pre-placed 4.7kΩ pull-ups (bridge top↔bottom halves of breadboard)
    "sda_pullup": ("I2C_SDA_R_A", "I2C_SDA_R_B"),
    "scl_pullup": ("I2C_SCL_R_A", "I2C_SCL_R_B"),
})
def test_sniff_i2c_rp2350b_write_transaction(app, jumperless, tmp_path: Path) -> None:
    """RP2350B sends I2C write to 0x50; spy captures START + addr + STOP."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.i2c", {
            "sda_pin": "dio0",
            "scl_pin": "dio1",
            "duration_s": 1.0,
            "clock_hz": 100_000,
            "poll_interval_s": 0.005,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()

    # i2c_configure + i2c_spy_start setup: 200ms is sufficient
    time.sleep(0.2)

    jumperless.exec("""
from machine import I2C, Pin
import time
i = I2C(0, sda=Pin(20), scl=Pin(21), freq=100000)
# NACK expected (no peripheral at 0x50) — spy still sees the transaction
try:
    i.writeto(0x50, b'\\x01\\x02')
except OSError:
    pass
time.sleep_ms(10)
""")

    t.join(timeout=2.0)
    assert not t.is_alive(), "sniff.i2c timed out"

    r = result["sniff"]
    assert r["artifact_error"] is None, f"artifact_error: {r['artifact_error']}"
    assert r["count"] > 0, "no I2C transactions captured"

    table = pq.read_table(r["artifact_path"])
    addresses = [row.as_py() for row in table.column("address")]
    assert 0x50 in addresses, f"expected address 0x50, got: {addresses}"
    types = [row.as_py() for row in table.column("type")]
    assert "write" in types, f"expected write transaction, got: {types}"


@pytest.mark.jumperless(connections={
    "rp_sda_to_ad3": ("GPIO_1", "DIO0"),
    "rp_scl_to_ad3": ("GPIO_2", "DIO1"),
    "sda_pullup": ("I2C_SDA_R_A", "I2C_SDA_R_B"),
    "scl_pullup": ("I2C_SCL_R_A", "I2C_SCL_R_B"),
})
def test_sniff_i2c_multiple_transactions(app, jumperless, tmp_path: Path) -> None:
    """RP2350B sends two distinct I2C transactions; spy captures both."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.i2c", {
            "sda_pin": "dio0",
            "scl_pin": "dio1",
            "duration_s": 1.0,
            "clock_hz": 100_000,
            "poll_interval_s": 0.005,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()
    time.sleep(0.2)

    jumperless.exec("""
from machine import I2C, Pin
import time
i = I2C(0, sda=Pin(20), scl=Pin(21), freq=100000)
for addr in (0x50, 0x60):
    try:
        i.writeto(addr, b'\\xAB')
    except OSError:
        pass
    time.sleep_ms(20)
""")

    t.join(timeout=2.0)
    assert not t.is_alive()

    r = result["sniff"]
    assert r["count"] >= 2, f"expected ≥2 transactions, got {r['count']}"
    table = pq.read_table(r["artifact_path"])
    addresses = {row.as_py() for row in table.column("address")}
    assert 0x50 in addresses and 0x60 in addresses, (
        f"expected 0x50 and 0x60, got: {addresses}"
    )
