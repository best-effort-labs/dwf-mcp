from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.decoder import Decoder as DecoderInstrument
from dwf_mcp.policy import SafetyPolicy
from tests.unit.test_can_decoder import _can_bits, _samples_from_bits, _stuff
from tests.unit.test_i2c_decoder import _i2c_samples
from tests.unit.test_spi_decoder import _spi_samples
from tests.unit.test_uart_decoder import _uart_samples


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def decoder(device: DwfDevice, tmp_path: Path) -> DecoderInstrument:
    device.open()
    return DecoderInstrument(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def _write_npz_with_sidecar(
    samples: np.ndarray, pins: list[str], sr: float, out: Path
) -> Path:
    """Write synthetic npz + sidecar in the format logic.capture produces."""
    npz_path = out / "capture.npz"
    arrays = {p: samples[:, i] for i, p in enumerate(pins)}
    np.savez_compressed(npz_path, **arrays)
    sidecar = {
        "instrument": "logic",
        "config": {"pins": pins, "sample_rate_hz": sr},
        "summary": {"sample_count": len(samples), "sample_rate_hz": sr},
    }
    npz_path.with_suffix(".json").write_text(json.dumps(sidecar))
    return npz_path


def test_decoder_spi_decodes_known_data(decoder: DecoderInstrument, tmp_path: Path) -> None:
    samples, _ = _spi_samples([0xA5, 0x5A])
    pins = ["dio0", "dio1", "dio2", "dio3"]
    npz_path = _write_npz_with_sidecar(samples, pins, 1_000_000.0, tmp_path)

    result = asyncio.run(decoder.spi(
        capture_path=str(npz_path),
        clk_pin="dio0", mosi_pin="dio1",
        miso_pin="dio2", cs_pin="dio3", mode=0,
    ))

    assert result["count"] == 2
    assert result["artifact_path"] is not None

    import pyarrow.parquet as pq
    table = pq.read_table(result["artifact_path"])
    mosi_col = [row.as_py() for row in table.column("mosi")]
    assert bytes([0xA5]) in mosi_col
    assert bytes([0x5A]) in mosi_col


def test_decoder_spi_missing_pin_returns_error(
    decoder: DecoderInstrument, tmp_path: Path
) -> None:
    samples, _ = _spi_samples([0xFF])
    npz_path = _write_npz_with_sidecar(samples, ["dio0", "dio1"], 1_000_000.0, tmp_path)

    result = asyncio.run(decoder.spi(
        capture_path=str(npz_path),
        clk_pin="dio0", mosi_pin="dio5",  # dio5 not captured
    ))
    assert "error" in result


def test_decoder_spi_missing_sample_rate_returns_error(
    decoder: DecoderInstrument, tmp_path: Path
) -> None:
    samples, _ = _spi_samples([0x01])
    npz_path = tmp_path / "bad.npz"
    np.savez_compressed(npz_path, dio0=samples[:, 0], dio1=samples[:, 1])
    # Sidecar without sample_rate_hz
    sidecar = {"config": {"pins": ["dio0", "dio1"]}}
    npz_path.with_suffix(".json").write_text(json.dumps(sidecar))

    result = asyncio.run(decoder.spi(capture_path=str(npz_path), clk_pin="dio0", mosi_pin="dio1"))
    assert "error" in result


def test_decoder_i2c_tool_writes_parquet(
    decoder: DecoderInstrument, tmp_path: Path
) -> None:
    """decoder.i2c reads an npz + sidecar, writes a decoded parquet."""
    import pyarrow.parquet as pq

    samples = _i2c_samples([(0x50, b"\x01\x02", True)])
    pins = ["dio0", "dio1"]
    npz_path = _write_npz_with_sidecar(samples, pins, 1_000_000.0, tmp_path)

    result = asyncio.run(decoder.i2c(
        capture_path=str(npz_path), sda_pin="dio0", scl_pin="dio1",
    ))
    assert result["artifact_error"] is None
    assert result["count"] == 1
    assert result["error_count"] == 0
    assert result["artifact_path"] is not None

    table = pq.read_table(result["artifact_path"])
    assert table.column("address")[0].as_py() == 0x50
    assert table.column("type")[0].as_py() == "write"
    assert table.column("data")[0].as_py() == b"\x01\x02"


def test_decoder_i2c_missing_pin_returns_error(
    decoder: DecoderInstrument, tmp_path: Path
) -> None:
    samples = _i2c_samples([(0x50, b"\x01", True)])
    npz_path = _write_npz_with_sidecar(samples, ["dio0", "dio1"], 1_000_000.0, tmp_path)

    result = asyncio.run(decoder.i2c(
        capture_path=str(npz_path), sda_pin="dio0", scl_pin="dio5",  # dio5 not captured
    ))
    assert "error" in result


def test_decoder_uart_tool_writes_parquet(
    decoder: DecoderInstrument, tmp_path: Path
) -> None:
    """decoder.uart decodes a synthetic UART capture and writes a parquet."""
    import pyarrow.parquet as pq

    samples = _uart_samples(b"Hi!", baud=9600, sample_rate_hz=96000.0)
    npz_path = _write_npz_with_sidecar(samples, ["dio0"], 96000.0, tmp_path)

    result = asyncio.run(decoder.uart(
        capture_path=str(npz_path), rx_pin="dio0", baud=9600,
    ))
    assert result["artifact_error"] is None
    assert result["count"] == 3
    assert result["error_count"] == 0
    assert result["artifact_path"] is not None

    table = pq.read_table(result["artifact_path"])
    payload = b"".join(row.as_py() for row in table.column("data"))
    assert payload == b"Hi!"


def test_decoder_can_tool_writes_parquet(
    decoder: DecoderInstrument, tmp_path: Path
) -> None:
    """decoder.can decodes a synthetic CAN frame and writes a parquet."""
    import pyarrow.parquet as pq

    bits = _stuff(_can_bits(0x123, b"\xDE\xAD"))
    samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    npz_path = _write_npz_with_sidecar(samples, ["dio0"], 2_000_000.0, tmp_path)

    result = asyncio.run(decoder.can(
        capture_path=str(npz_path), rx_pin="dio0", bitrate=100_000,
    ))
    assert result["artifact_error"] is None
    assert result["count"] == 1
    assert result["error_count"] == 0
    assert result["artifact_path"] is not None

    table = pq.read_table(result["artifact_path"])
    assert table.column("frame_id")[0].as_py() == 0x123
    assert table.column("data")[0].as_py() == b"\xDE\xAD"
    assert table.column("dlc")[0].as_py() == 2
    assert table.column("crc_valid")[0].as_py() is True
