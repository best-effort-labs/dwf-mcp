"""SPI sniff hardware test — RP2350B onboard the Jumperless as external SPI master.

The AD3's own protocol.spi master CANNOT be used: protocol.spi.writeRead
internally uses DigitalIn to sample MISO, which clobbers the user-configured
DigitalIn record-mode state set up by sniff.spi_start. The two hardware blocks
share the same sampling buffer on the AD3.

So we use the Jumperless's onboard RP2350B as an external SPI master, sniff
on the AD3's DigitalIn, and decode in software. Same pattern as the
sniff.{i2c,uart} observe-mode hardware tests.

Wiring (via Jumperless, automatic):
  AD3_GND → GND                   (shared reference)
  GPIO_3 (RP2350B GP22, SCK)  → DIO0
  GPIO_4 (RP2350B GP23, MOSI) → DIO1
  GPIO_2 (RP2350B GP21, CS)   → DIO3
  DIO1 → DIO2                    (MOSI loopback to MISO so the decoder
                                   sees identical bytes on both)

RP2350B-side: machine.SPI(0, sck=Pin(22), mosi=Pin(23)) — RP2350B SPI0
alternate function on GP22/GP23. The MISO pin on the RP2350B is left
unmapped (the master doesn't actually need to receive anything; the
AD3-side loopback is what the sniff decoder sees).

Run:
  pytest tests/hardware/test_sniff_spi_hardware.py -v -m hardware
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
    pytest.mark.requires(instruments={"sniff"}),
]


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("sniff_spi")),
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


_RP2350B_SPI_DRIVE = """
from machine import SPI, Pin
spi = SPI(0, baudrate=100_000, polarity=0, phase=0,
          sck=Pin(22), mosi=Pin(23), miso=Pin(20))
cs = Pin(21, Pin.OUT, value=1)
cs.value(0)
spi.write({payload})
cs.value(1)
"""


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={
    "gnd_bridge": ("AD3_GND", "GND"),
    "sck":        ("GPIO_3", "DIO0"),
    "mosi":       ("GPIO_4", "DIO1"),
    "cs":         ("GPIO_2", "DIO3"),
    "miso_loop":  ("DIO1", "DIO2"),
})
async def test_sniff_spi_captures_external_transfer(app, jumperless, tmp_path: Path) -> None:
    """RP2350B SPI master sends 2 bytes; sniff.spi captures and decodes them."""
    if jumperless is None:
        pytest.skip("Jumperless not available — sniff.spi needs an external SPI master")

    start = await app.call_tool("sniff.spi_start", {
        "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
        "cs_pin": "dio3", "mode": 0, "freq_hz": 100_000, "max_duration_s": 1.0,
    })
    sniff_id = start["sniff_id"]
    # Let DigitalIn record_loop arm + poll at least once before firing stimulus.
    await asyncio.sleep(0.1)

    await asyncio.to_thread(
        jumperless.exec, _RP2350B_SPI_DRIVE.format(payload="bytes([0xA5, 0x5A])"),
    )
    # Let record_loop drain captured samples before stopping.
    await asyncio.sleep(0.1)

    result = await app.call_tool("sniff.spi_stop", {"sniff_id": sniff_id})

    assert result["artifact_error"] is None, f"artifact_error: {result['artifact_error']}"
    assert result["artifact_path"] is not None
    assert result["count"] >= 2, f"expected ≥2 decoded words, got {result['count']}"

    table = pq.read_table(result["artifact_path"])
    mosi_bytes = [row.as_py() for row in table.column("mosi")]
    assert bytes([0xA5]) in mosi_bytes, f"0xA5 not found in MOSI: {mosi_bytes!r}"
    assert bytes([0x5A]) in mosi_bytes, f"0x5A not found in MOSI: {mosi_bytes!r}"

    # With DIO1→DIO2 loopback the decoded MISO should equal MOSI.
    miso_bytes = [row.as_py() for row in table.column("miso")]
    for mo, mi in zip(mosi_bytes, miso_bytes):
        assert mo == mi, f"MOSI/MISO mismatch: {mo!r} != {mi!r}"


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={
    "gnd_bridge": ("AD3_GND", "GND"),
    "sck":        ("GPIO_3", "DIO0"),
    "mosi":       ("GPIO_4", "DIO1"),
    "cs":         ("GPIO_2", "DIO3"),
    "miso_loop":  ("DIO1", "DIO2"),
})
async def test_sniff_spi_lost_samples_zero(app, jumperless, tmp_path: Path) -> None:
    """Short 4-byte capture should report zero lost samples."""
    if jumperless is None:
        pytest.skip("Jumperless not available — sniff.spi needs an external SPI master")

    start = await app.call_tool("sniff.spi_start", {
        "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
        "cs_pin": "dio3", "mode": 0, "freq_hz": 100_000, "max_duration_s": 1.0,
    })
    await asyncio.sleep(0.1)

    await asyncio.to_thread(
        jumperless.exec, _RP2350B_SPI_DRIVE.format(payload="bytes([0xFF, 0x00, 0xAA, 0x55])"),
    )
    await asyncio.sleep(0.1)

    result = await app.call_tool("sniff.spi_stop", {"sniff_id": start["sniff_id"]})

    assert result["lost_samples"] == 0, f"lost_samples={result['lost_samples']}"
    assert result["count"] >= 4, f"expected ≥4 decoded words, got {result['count']}"
