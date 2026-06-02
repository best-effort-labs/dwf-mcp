from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary


@pytest.fixture
def writer(tmp_path: Path) -> ArtifactWriter:
    return ArtifactWriter(workspace=tmp_path)


def test_writer_creates_captures_dir(writer: ArtifactWriter, tmp_path: Path) -> None:
    assert (tmp_path / "captures").is_dir()


def test_write_npz_returns_path_and_sidecar(writer: ArtifactWriter, tmp_path: Path) -> None:
    samples = np.arange(100, dtype=np.float32)
    summary = CaptureSummary(
        instrument="scope",
        sample_count=100,
        sample_rate_hz=1_000_000,
        extra={"min": 0.0, "max": 99.0},
    )
    result = writer.write_npz(
        instrument="scope",
        arrays={"ch1": samples},
        config={"channels": [1], "sample_rate_hz": 1_000_000},
        summary=summary,
    )
    assert Path(result.path).is_file()
    assert Path(result.sidecar_path).is_file()
    assert Path(result.path).parent == tmp_path / "captures"

    loaded = np.load(result.path)
    assert np.array_equal(loaded["ch1"], samples)

    sidecar = json.loads(Path(result.sidecar_path).read_text())
    assert sidecar["instrument"] == "scope"
    assert sidecar["config"] == {"channels": [1], "sample_rate_hz": 1_000_000}
    assert sidecar["summary"]["sample_count"] == 100
    assert sidecar["summary"]["extra"]["max"] == 99.0


def test_explicit_output_path_overrides_default(writer: ArtifactWriter, tmp_path: Path) -> None:
    target = tmp_path / "custom.npz"
    result = writer.write_npz(
        instrument="scope",
        arrays={"ch1": np.zeros(10)},
        config={},
        summary=CaptureSummary(instrument="scope", sample_count=10),
        output_path=target,
    )
    assert Path(result.path) == target
    assert Path(result.sidecar_path) == target.with_suffix(".json")


def test_default_workspace_is_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / "fake-temp"))
    (tmp_path / "fake-temp").mkdir()
    writer = ArtifactWriter()
    assert Path(writer.workspace).name == "fake-temp"
