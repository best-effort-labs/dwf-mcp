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
from tests.unit.test_spi_decoder import _spi_samples


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
