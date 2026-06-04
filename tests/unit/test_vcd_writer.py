"""Round-trip test for vcd_writer: write a synthetic uint8 array, read it back."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

vcd = pytest.importorskip("vcd")


def test_vcd_writer_round_trips_transitions(tmp_path: Path) -> None:
    from dwf_mcp.vcd_writer import write as vcd_write

    # 4 samples, 2 pins. Pin 0 toggles at sample 2; pin 1 stays low.
    samples = np.array(
        [[0, 0], [0, 0], [1, 0], [1, 0]],
        dtype=np.uint8,
    )
    pin_names = ["dio0", "dio1"]
    out_path = tmp_path / "test.vcd"
    vcd_write(out_path, samples, pin_names, sample_rate_hz=1_000_000.0)

    assert out_path.exists()
    content = out_path.read_text()
    # VCD file must contain variable declarations and a time step
    assert "$var" in content
    assert "dio0" in content
    assert "dio1" in content
    # The toggle at sample 2 → timescale 1us → time 2us
    assert "#2" in content


def test_vcd_writer_missing_package_raises(tmp_path: Path, monkeypatch) -> None:
    import dwf_mcp.vcd_writer as vw
    monkeypatch.setattr(vw, "HAS_VCD", False)

    samples = np.zeros((4, 2), dtype=np.uint8)
    with pytest.raises(ImportError, match="pyvcd"):
        vw.write(tmp_path / "out.vcd", samples, ["a", "b"], 1_000_000.0)


def test_vcd_stream_writer_single_chunk_matches_oneshot(tmp_path: Path) -> None:
    """VcdStreamWriter with one full chunk produces identical output to write()."""
    from dwf_mcp.vcd_writer import VcdStreamWriter, write as vcd_write

    samples = np.array([[0, 0], [0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.uint8)
    pin_names = ["dio0", "dio1"]
    rate = 1_000_000.0

    path_oneshot = tmp_path / "oneshot.vcd"
    vcd_write(path_oneshot, samples, pin_names, rate)

    path_stream = tmp_path / "stream.vcd"
    with VcdStreamWriter(path_stream, pin_names, rate) as w:
        w.write_chunk(samples)

    assert path_stream.read_text() == path_oneshot.read_text()


def test_vcd_stream_writer_multi_chunk_matches_oneshot(tmp_path: Path) -> None:
    """VcdStreamWriter split across two chunks produces same output as write() on full array."""
    from dwf_mcp.vcd_writer import VcdStreamWriter, write as vcd_write

    samples = np.array(
        [[1, 0], [1, 1], [0, 1], [0, 0], [1, 0], [0, 0]], dtype=np.uint8
    )
    pin_names = ["dio0", "dio1"]
    rate = 1_000_000.0

    path_oneshot = tmp_path / "oneshot.vcd"
    vcd_write(path_oneshot, samples, pin_names, rate)

    path_stream = tmp_path / "stream.vcd"
    with VcdStreamWriter(path_stream, pin_names, rate) as w:
        w.write_chunk(samples[:3])
        w.write_chunk(samples[3:])

    assert path_stream.read_text() == path_oneshot.read_text()


def test_vcd_stream_writer_sample_counter_advances(tmp_path: Path) -> None:
    """Sample counter correctly advances so chunk-boundary timestamps are correct."""
    from dwf_mcp.vcd_writer import VcdStreamWriter

    # Chunk 1: no transitions at t=0,1,2
    chunk1 = np.array([[0, 0], [0, 0], [0, 0]], dtype=np.uint8)
    # Chunk 2: transition at t=3 (index 0 of chunk2 → global t=3)
    chunk2 = np.array([[1, 0], [1, 0]], dtype=np.uint8)

    path = tmp_path / "counter.vcd"
    with VcdStreamWriter(path, ["dio0", "dio1"], 1_000_000.0) as w:
        w.write_chunk(chunk1)
        w.write_chunk(chunk2)

    content = path.read_text()
    assert "#3" in content  # transition at global t=3


def test_vcd_stream_writer_close_is_idempotent(tmp_path: Path) -> None:
    from dwf_mcp.vcd_writer import VcdStreamWriter

    path = tmp_path / "idem.vcd"
    w = VcdStreamWriter(path, ["dio0"], 1_000_000.0)
    w.write_chunk(np.array([[0], [1]], dtype=np.uint8))
    w.close()
    w.close()  # second close must not raise


def test_vcd_stream_writer_missing_package_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dwf_mcp.vcd_writer as vw
    monkeypatch.setattr(vw, "HAS_VCD", False)
    with pytest.raises(ImportError, match="pyvcd"):
        vw.VcdStreamWriter(tmp_path / "out.vcd", ["a"], 1_000_000.0)
