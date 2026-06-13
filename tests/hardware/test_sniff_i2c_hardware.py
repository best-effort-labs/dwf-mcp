"""I2C sniff hardware test — stimulus from RP2350B onboard the Jumperless.

The JumperlOS examples directory has no documented I2C example (only the OLED,
which is firmware-managed). Empirically, ``machine.SoftI2C`` hangs silently
on this Jumperless MicroPython build, but hardware ``machine.I2C(0)`` works
with explicit pin overrides. We use 10kHz (instead of 100kHz) because the
10kΩ pull-ups give marginal rise times at higher rates.

Wiring (via Jumperless, automatic):
  TOP_RAIL set to 3.3V (so the pull-ups don't over-voltage the RP2350B's
    3.3V-max GP20/GP21)
  TOP_RAIL → I2C_SDA_R_A; I2C_SDA_R_B → DIO0 (4.7kΩ SDA pull-up to 3.3V)
  TOP_RAIL → I2C_SCL_R_A; I2C_SCL_R_B → DIO1 (4.7kΩ SCL pull-up to 3.3V)
  GPIO_1 (RP2350B GP20, SDA) → DIO0
  GPIO_2 (RP2350B GP21, SCL) → DIO1
  AD3_GND                     → GND (shared reference)

No peripheral at 0x50 — the RP2350B gets NACK after the address byte, but the
AD3 spy still captures the transaction (START + addr + NACK + STOP).

Run:
  pytest tests/hardware/test_sniff_i2c_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
import pyarrow.parquet as pq

from tests.hardware.conftest import wait_for_sniff_claim

pytestmark = [pytest.mark.hardware, pytest.mark.device_config("max_digital_in")]


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


@pytest.fixture(scope="module", autouse=True)
def rail_3v3(jumperless):
    """Set TOP_RAIL to 3.3V so pull-ups don't over-voltage the RP2350B's
    3.3V-max GPIO. Restore to 5V default after."""
    if jumperless is None:
        yield
        return
    jumperless.dac_set("TOP_RAIL", 3.3)
    yield
    jumperless.dac_set("TOP_RAIL", 5.0)


@pytest.mark.jumperless(connections={
    "gnd_bridge":    ("AD3_GND", "GND"),
    # Pull-ups: 4.7kΩ resistor between TOP_RAIL (set to 3.3V) and signal line
    "sda_pwr":       ("TOP_RAIL", "I2C_SDA_R_A"),
    "sda_sig":       ("I2C_SDA_R_B", "DIO0"),
    "scl_pwr":       ("TOP_RAIL", "I2C_SCL_R_A"),
    "scl_sig":       ("I2C_SCL_R_B", "DIO1"),
    # RP2350B drives I2C master
    "rp_sda_to_ad3": ("GPIO_1", "DIO0"),
    "rp_scl_to_ad3": ("GPIO_2", "DIO1"),
})
def test_sniff_i2c_rp2350b_write_transaction(app, jumperless, tmp_path: Path) -> None:
    """RP2350B sends I2C write to 0x50 via SoftI2C; spy captures START + addr + STOP."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.i2c", {
            "sda_pin": "dio0",
            "scl_pin": "dio1",
            "duration_s": 1.0,
            "clock_hz": 100_000,
            "poll_interval_s": 0.002,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()
    wait_for_sniff_claim(app, "sniff_i2c")

    jumperless.exec("""
from machine import I2C, Pin
import time
i = I2C(0, scl=Pin(21), sda=Pin(20), freq=10000)
# NACK expected (no peripheral at 0x50) — spy still sees the transaction
try:
    i.writeto(0x50, b'\\x01\\x02')
except OSError:
    pass
time.sleep_ms(50)
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
    "gnd_bridge":    ("AD3_GND", "GND"),
    # Pull-ups: 4.7kΩ resistor between TOP_RAIL (set to 3.3V) and signal line
    "sda_pwr":       ("TOP_RAIL", "I2C_SDA_R_A"),
    "sda_sig":       ("I2C_SDA_R_B", "DIO0"),
    "scl_pwr":       ("TOP_RAIL", "I2C_SCL_R_A"),
    "scl_sig":       ("I2C_SCL_R_B", "DIO1"),
    # RP2350B drives I2C master
    "rp_sda_to_ad3": ("GPIO_1", "DIO0"),
    "rp_scl_to_ad3": ("GPIO_2", "DIO1"),
})
def test_sniff_i2c_multiple_transactions(app, jumperless, tmp_path: Path) -> None:
    """RP2350B sends two distinct I2C transactions via SoftI2C; spy captures both."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    result: dict = {}

    def run_sniff() -> None:
        result["sniff"] = asyncio.run(app.call_tool("sniff.i2c", {
            "sda_pin": "dio0",
            "scl_pin": "dio1",
            "duration_s": 1.0,
            "clock_hz": 100_000,
            "poll_interval_s": 0.002,
        }))

    t = threading.Thread(target=run_sniff)
    t.start()
    wait_for_sniff_claim(app, "sniff_i2c")

    jumperless.exec("""
from machine import I2C, Pin
import time
i = I2C(0, scl=Pin(21), sda=Pin(20), freq=10000)
for addr in (0x50, 0x60):
    try:
        i.writeto(addr, b'\\xAB')
    except OSError:
        pass
    time.sleep_ms(30)
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
