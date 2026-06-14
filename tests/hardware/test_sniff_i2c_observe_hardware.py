"""I2C observe-mode sniff hardware test — RP2350B drives the I2C master,
the AD3 ``sniff.i2c_start/stop`` tools capture via DigitalIn observe-mode.

Unlike ``test_sniff_i2c_hardware.py`` (which uses the protocol.i2c spy engine
and claims ``i2c_engine`` + the DIO pins), the observe-mode tools use
DigitalIn streaming and do NOT claim the engine, so a concurrent master
(e.g. ``i2c.configure`` + ``i2c.scan``) can run simultaneously. The
coexistence test exercises that spec headline feature.

Wiring (via Jumperless, automatic):
  TOP_RAIL set to 3.3V (so the 4.7kΩ pull-ups don't over-voltage the RP2350B's
    3.3V-max GP20/GP21)
  TOP_RAIL → I2C_SDA_R_A; I2C_SDA_R_B → DIO0
  TOP_RAIL → I2C_SCL_R_A; I2C_SCL_R_B → DIO1
  GPIO_1 (RP2350B GP20, SDA) → DIO0
  GPIO_2 (RP2350B GP21, SCL) → DIO1
  AD3_GND                     → GND (shared reference)

Run:
  pytest tests/hardware/test_sniff_i2c_observe_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.device_config("max_digital_in")]


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("sniff_i2c_observe")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app, request):
    # Honor DWF_TEST_SERIAL (target the wired DUT, not SDK default idx 0) and the
    # module-level device_config marker. Without this, the test silently runs on
    # an unwired device at the default config.
    args = {}
    serial = os.environ.get("DWF_TEST_SERIAL")
    if serial:
        args["device_serial"] = serial
    marker = request.node.get_closest_marker("device_config")
    if marker:
        args["device_config"] = marker.args[0]
    result = asyncio.run(app.call_tool("waveforms.open", args))
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


_I2C_WIRING = {
    "gnd_bridge":    ("AD3_GND", "GND"),
    # Pull-ups: 4.7kΩ resistor between TOP_RAIL (set to 3.3V) and signal line
    "sda_pwr":       ("TOP_RAIL", "I2C_SDA_R_A"),
    "sda_sig":       ("I2C_SDA_R_B", "DIO0"),
    "scl_pwr":       ("TOP_RAIL", "I2C_SCL_R_A"),
    "scl_sig":       ("I2C_SCL_R_B", "DIO1"),
    # RP2350B drives I2C master
    "rp_sda_to_ad3": ("GPIO_1", "DIO0"),
    "rp_scl_to_ad3": ("GPIO_2", "DIO1"),
}


_RP2350B_I2C_WRITE = """
from machine import I2C, Pin
import time
i = I2C(0, scl=Pin(21), sda=Pin(20), freq=10000)
# NACK expected (no peripheral at 0x50) — spy still sees the transaction
try:
    i.writeto(0x50, b'\\x01\\x02')
except OSError:
    pass
time.sleep_ms(50)
"""


@pytest.mark.asyncio
@pytest.mark.jumperless(connections=_I2C_WIRING)
async def test_sniff_i2c_observe_captures_rp2350b_write(
    app, jumperless, tmp_path: Path,
) -> None:
    """sniff.i2c_start + RP2350B master write + sniff.i2c_stop decodes addr 0x50."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    start_result = await app.call_tool("sniff.i2c_start", {
        "sda_pin": "dio0",
        "scl_pin": "dio1",
        "clock_hz": 100_000,
        "max_duration_s": 2.0,
    })
    sniff_id = start_result["sniff_id"]

    # Let the record_loop spin up and arm DigitalIn before stimulus.
    await asyncio.sleep(0.2)

    # Fire stimulus on a worker thread so the event loop keeps polling DigitalIn.
    await asyncio.to_thread(jumperless.exec, _RP2350B_I2C_WRITE)

    # Allow trailing samples to drain.
    await asyncio.sleep(0.1)

    stop_result = await app.call_tool("sniff.i2c_stop", {"sniff_id": sniff_id})

    assert stop_result["artifact_error"] is None, (
        f"artifact_error: {stop_result['artifact_error']}"
    )
    assert stop_result["artifact_path"] is not None
    assert stop_result["count"] > 0, "no I2C transactions captured"

    table = pq.read_table(stop_result["artifact_path"])
    addresses = [row.as_py() for row in table.column("address")]
    assert 0x50 in addresses, f"expected address 0x50, got: {addresses}"


@pytest.mark.asyncio
@pytest.mark.jumperless(connections=_I2C_WIRING)
async def test_sniff_i2c_observe_coexists_with_master(
    app, jumperless, tmp_path: Path,
) -> None:
    """sniff.i2c_start (observe) does NOT block i2c.configure + i2c.scan.

    This is the headline feature of the observe-mode tools: the protocol engine
    and DIO pins remain available because the observer claims only the
    DigitalIn-streaming resource via claim_observe.
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    start_result = await app.call_tool("sniff.i2c_start", {
        "sda_pin": "dio0",
        "scl_pin": "dio1",
        "clock_hz": 100_000,
        "max_duration_s": 2.0,
    })
    sniff_id = start_result["sniff_id"]

    # Let the observer arm.
    await asyncio.sleep(0.1)

    # The KEY assertion: these must succeed despite the sniffer running on the
    # same DIO pins. Both use the protocol.i2c engine on dio0/dio1.
    await app.call_tool("i2c.configure", {
        "sda_pin": "dio0",
        "scl_pin": "dio1",
        "clock_hz": 100_000,
    })
    scan_result = await app.call_tool("i2c.scan", {})

    assert isinstance(scan_result.get("found"), list), (
        f"i2c.scan did not return a 'found' list: {scan_result!r}"
    )

    # Let the scan transactions land in the observer's buffer.
    await asyncio.sleep(0.1)

    stop_result = await app.call_tool("sniff.i2c_stop", {"sniff_id": sniff_id})

    assert stop_result["artifact_error"] is None, (
        f"artifact_error: {stop_result['artifact_error']}"
    )
    assert stop_result["artifact_path"] is not None, (
        "sniff.i2c_stop returned no artifact path"
    )
