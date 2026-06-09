"""SPI sniff hardware test.

CURRENTLY SKIPPED — AD3 hardware limitation.

The original test premise was that ``sniff.spi_start`` (DigitalIn observer)
could coexist with ``spi.configure`` + ``spi.transfer`` (protocol.spi engine)
because the AD3 docs treat them as independent hardware blocks. Empirically,
``protocol.spi.writeRead`` internally uses DigitalIn to sample MISO, so
calling ``spi.transfer`` resets the DigitalIn record-mode state we set up
in ``sniff.spi_start``. The observer ends up with zero samples.

Diagnostic evidence (2026-06-08):
  - DigitalIn record mode captures samples cleanly when no protocol.spi call
    is made (no `lost`/`reset` after stimulus).
  - As soon as ``spi.transfer`` runs, the next ``logic_record_status`` returns
    ``(available=0, lost=0, remaining=0)`` — full reset of DigitalIn state.

To actually exercise ``sniff.spi`` on hardware, route an EXTERNAL SPI master
(another microcontroller, USB-SPI dongle, etc.) into DIO0..DIO3 and use only
``sniff.spi_*`` on the AD3. Same pattern as ``test_sniff_can_hardware.py``,
which is skipped pending an external CAN transceiver.

Wiring (if/when external master available):
  DIO0 = CLK  (external master output)
  DIO1 = MOSI (external master output)
  DIO2 = MISO (external slave output, can be tied LOW for passive sniff)
  DIO3 = CS   (external master output, active-low)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("sniff_spi")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    result = asyncio.run(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.run(app.call_tool("waveforms.close", {}))


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={"mosi_miso_loop": ("DIO1", "DIO2")})
async def test_sniff_spi_captures_active_transfer(app, tmp_path: Path) -> None:
    """sniff.spi_start + spi.transfer + sniff.spi_stop decodes known data."""
    pytest.skip(
        "AD3 protocol.spi.writeRead resets DigitalIn state — see module docstring. "
        "Requires external SPI master to exercise."
    )
    await app.call_tool("spi.configure", {
        "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
        "cs_pin": "dio3", "mode": 0, "frequency_hz": 100_000,
    })

    start = await app.call_tool("sniff.spi_start", {
        "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
        "cs_pin": "dio3", "mode": 0, "freq_hz": 100_000,
    })
    sniff_id = start["sniff_id"]

    xfer = await app.call_tool("spi.transfer", {"data": [0xA5, 0x5A]})
    assert xfer["sent"] == [0xA5, 0x5A]

    result = await app.call_tool("sniff.spi_stop", {"sniff_id": sniff_id})

    assert result["artifact_error"] is None, f"artifact_error: {result['artifact_error']}"
    assert result["artifact_path"] is not None
    assert result["count"] >= 2, f"expected ≥2 decoded words, got {result['count']}"

    import pyarrow.parquet as pq
    table = pq.read_table(result["artifact_path"])
    mosi_bytes = [row.as_py() for row in table.column("mosi")]
    assert bytes([0xA5]) in mosi_bytes, "0xA5 not found in decoded MOSI"
    assert bytes([0x5A]) in mosi_bytes, "0x5A not found in decoded MOSI"

    miso_bytes = [row.as_py() for row in table.column("miso")]
    for mo, mi in zip(mosi_bytes, miso_bytes):
        assert mo == mi, f"MOSI/MISO mismatch: {mo!r} != {mi!r}"


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={"mosi_miso_loop": ("DIO1", "DIO2")})
async def test_sniff_spi_lost_samples_zero(app, tmp_path: Path) -> None:
    """Verify no samples are lost during a short capture."""
    pytest.skip(
        "AD3 protocol.spi.writeRead resets DigitalIn state — see module docstring. "
        "Requires external SPI master to exercise."
    )
    await app.call_tool("spi.configure", {
        "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
        "cs_pin": "dio3", "mode": 0, "frequency_hz": 100_000,
    })
    start = await app.call_tool("sniff.spi_start", {
        "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2",
        "cs_pin": "dio3", "mode": 0, "freq_hz": 100_000,
    })
    await app.call_tool("spi.transfer", {"data": [0xFF, 0x00, 0xAA, 0x55]})
    result = await app.call_tool("sniff.spi_stop", {"sniff_id": start["sniff_id"]})

    assert result["lost_samples"] == 0, f"lost_samples={result['lost_samples']}"
    assert result["count"] >= 4
