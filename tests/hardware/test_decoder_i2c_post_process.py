"""Post-process decoder hardware test — capture raw DIO samples with
``logic.record_start/stop``, then decode the saved npz with ``decoder.i2c``.

This is the post-process path: ``logic.record_*`` writes a per-pin column
npz + JSON sidecar (with ``pins`` and ``sample_rate_hz``); ``decoder.i2c``
reads those, reconstructs a 16-column sample array, and runs the
I2cDecoder offline.

Wiring (same as ``test_sniff_i2c_observe_hardware.py``):
  TOP_RAIL set to 3.3V (so the 4.7kΩ pull-ups don't over-voltage the RP2350B's
    3.3V-max GP20/GP21)
  TOP_RAIL → I2C_SDA_R_A; I2C_SDA_R_B → DIO0
  TOP_RAIL → I2C_SCL_R_A; I2C_SCL_R_B → DIO1
  GPIO_1 (RP2350B GP20, SDA) → DIO0
  GPIO_2 (RP2350B GP21, SCL) → DIO1
  AD3_GND                     → GND

Run:
  pytest tests/hardware/test_decoder_i2c_post_process.py -v -m hardware
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.device_config("max_digital_in"),
    pytest.mark.requires(instruments={"decoder"}),
]


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("decoder_i2c_pp")),
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


_RP2350B_I2C_WRITE = """
from machine import I2C, Pin
import time
i = I2C(0, scl=Pin(21), sda=Pin(20), freq=10000)
try:
    i.writeto(0x50, b'\\x01\\x02')
except OSError:
    pass
time.sleep_ms(50)
"""


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={
    "gnd_bridge":    ("AD3_GND", "GND"),
    "sda_pwr":       ("TOP_RAIL", "I2C_SDA_R_A"),
    "sda_sig":       ("I2C_SDA_R_B", "DIO0"),
    "scl_pwr":       ("TOP_RAIL", "I2C_SCL_R_A"),
    "scl_sig":       ("I2C_SCL_R_B", "DIO1"),
    "rp_sda_to_ad3": ("GPIO_1", "DIO0"),
    "rp_scl_to_ad3": ("GPIO_2", "DIO1"),
})
async def test_decoder_i2c_processes_real_capture(
    app, jumperless, tmp_path: Path,
) -> None:
    """logic.record_* captures DIO0/DIO1 → decoder.i2c decodes addr 0x50 offline."""
    if jumperless is None:
        pytest.skip("Jumperless not available")

    # 1. Start raw logic capture on the two I2C lines.
    start = await app.call_tool("logic.record_start", {
        "pins": ["dio0", "dio1"],
        "sample_rate_hz": 1_000_000.0,
        "duration_s": 2.0,
    })
    record_id = start["record_id"]

    # Let the record_loop spin up before stimulus.
    await asyncio.sleep(0.2)

    # 2. Fire the RP2350B I2C write on a worker thread.
    await asyncio.to_thread(jumperless.exec, _RP2350B_I2C_WRITE)

    # Allow trailing samples to drain.
    await asyncio.sleep(0.1)

    # 3. Stop the capture; this writes the npz + JSON sidecar.
    stop = await app.call_tool("logic.record_stop", {"record_id": record_id})

    assert stop["artifact_error"] is None, (
        f"artifact_error: {stop['artifact_error']}"
    )
    capture_path = stop["artifact_path"]
    assert capture_path is not None, "logic.record_stop returned no artifact_path"
    assert Path(capture_path).exists(), f"capture file missing: {capture_path}"

    # 4. Post-process the saved capture with decoder.i2c.
    decode_result = await app.call_tool("decoder.i2c", {
        "capture_path": capture_path,
        "sda_pin": "dio0",
        "scl_pin": "dio1",
    })

    assert "error" not in decode_result, (
        f"decoder.i2c returned error: {decode_result.get('error')!r}"
    )
    assert decode_result["artifact_error"] is None, (
        f"artifact_error: {decode_result['artifact_error']}"
    )
    assert decode_result["artifact_path"] is not None
    assert decode_result["count"] > 0, "no I2C transactions decoded from capture"

    # 5. Verify the decoded parquet contains address 0x50.
    table = pq.read_table(decode_result["artifact_path"])
    addresses = [row.as_py() for row in table.column("address")]
    assert 0x50 in addresses, f"expected address 0x50, got: {addresses}"
