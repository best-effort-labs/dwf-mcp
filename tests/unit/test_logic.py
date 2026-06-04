from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.logic import Logic
from dwf_mcp.policy import SafetyPolicy


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
def logic(device: DwfDevice, tmp_path: Path) -> Logic:
    device.open()
    return Logic(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


# --- Buffer-mode tests ---

def test_configure_claims_pins(logic: Logic) -> None:
    logic.configure(pins=["dio0", "dio1"], sample_rate_hz=1_000_000, buffer_size=1024)
    claimed = logic.device.allocator.claimed_pins()
    assert "dio0" in claimed and "dio1" in claimed


def test_configure_calls_backend(logic: Logic) -> None:
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    cfgs = [c for c in fake.logic_calls if c[0] == "configure"]
    assert len(cfgs) == 1
    assert cfgs[0][1]["pin_mask"] == 0b1  # dio0 = bit 0
    assert cfgs[0][1]["buffer_size"] == 1024


def test_configure_partial_failure_releases_claim(
    logic: Logic, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("backend exploded")
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    monkeypatch.setattr(fake, "logic_configure", boom)
    with pytest.raises(RuntimeError):
        logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    assert logic.device.allocator.claimed_pins() == {}


def test_capture_writes_npz_artifact(logic: Logic, tmp_path: Path) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Provide canned data: 1024 samples, 16 channels, dio0 = 1
    data = np.zeros((1024, 16), dtype=np.uint8)
    data[:, 0] = 1
    fake._logic_canned_data = data
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    out_path = tmp_path / "logic_test.npz"
    result = logic.capture(output_path=str(out_path))
    assert "path" in result
    assert Path(result["path"]).exists()
    loaded = np.load(result["path"])
    # Should have 'dio0' key with shape (1024,)
    assert "dio0" in loaded
    assert loaded["dio0"].shape == (1024,)
    assert all(loaded["dio0"] == 1)


def test_capture_pin_claim_held_after_capture(logic: Logic) -> None:
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=1024)
    logic.capture()
    # Claim is held until instrument.release() — NOT released after capture.
    assert "dio0" in logic.device.allocator.claimed_pins()


def test_capture_invokes_vcd_writer(logic: Logic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dwf_mcp.vcd_writer as vw
    calls = []
    def fake_write(path, samples, pin_names, sample_rate_hz):
        calls.append((path, samples, pin_names, sample_rate_hz))
    monkeypatch.setattr(vw, "write", fake_write)
    monkeypatch.setattr(vw, "HAS_VCD", True)
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    fake._logic_canned_data = np.zeros((64, 16), dtype=np.uint8)
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=64)
    logic.capture(output_path=str(tmp_path / "out.vcd"), format="vcd")
    assert len(calls) == 1
    assert calls[0][2] == ["dio0"]


def test_capture_vcd_missing_package_raises(logic: Logic, monkeypatch: pytest.MonkeyPatch) -> None:
    import dwf_mcp.vcd_writer as vw
    monkeypatch.setattr(vw, "HAS_VCD", False)
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=64)
    with pytest.raises(ImportError, match="pyvcd"):
        logic.capture(format="vcd")
