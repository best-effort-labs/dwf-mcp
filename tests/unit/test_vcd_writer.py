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
