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


# --- Streaming (record) tests ---

@pytest.mark.asyncio
async def test_record_start_returns_record_id(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Single poll: 10 available, 0 lost, 0 remaining → loop exits after one iteration.
    fake.set_logic_record_status_sequence([(10, 0, 0)])
    result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    assert "record_id" in result
    assert isinstance(result["record_id"], str)
    # Clean up.
    await logic.record_stop(record_id=result["record_id"])


@pytest.mark.asyncio
async def test_record_status_reports_done(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    fake.set_logic_record_status_sequence([(5, 0, 0)])
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.005
    )
    rid = start_result["record_id"]
    # Give the background task time to run.
    await asyncio.sleep(0.05)
    status = logic.record_status(record_id=rid)
    assert status["record_id"] == rid
    assert "done" in status
    await logic.record_stop(record_id=rid)


@pytest.mark.asyncio
async def test_record_stop_writes_artifact(logic: Logic, tmp_path: Path) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Provide canned chunk data.
    fake._logic_record_canned_chunk = np.zeros((10, 16), dtype=np.uint8)
    fake._logic_record_canned_chunk[:, 0] = 1  # dio0 high
    fake.set_logic_record_status_sequence([(10, 0, 0)])
    out_path = tmp_path / "rec.npz"
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01,
        output_path=str(out_path),
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.05)
    stop_result = await logic.record_stop(record_id=rid)
    assert stop_result["error"] is None
    assert stop_result["artifact_path"] is not None
    assert Path(stop_result["artifact_path"]).exists()


@pytest.mark.asyncio
async def test_record_lost_samples_counted(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    # Sequence: first poll has 5 available + 3 lost, then 5 available + 0 lost + remaining=0.
    fake.set_logic_record_status_sequence([(5, 3, 1), (5, 0, 0)])
    fake._logic_record_canned_chunk = np.zeros((10, 16), dtype=np.uint8)
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.1)
    stop_result = await logic.record_stop(record_id=rid)
    assert stop_result["lost_samples"] >= 3


@pytest.mark.asyncio
async def test_record_backend_exception_sets_error(
    logic: Logic, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    call_count = [0]
    def boom_on_second(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise RuntimeError("simulated backend failure")
        return (5, 0, 1)  # remaining=1 to keep loop running
    monkeypatch.setattr(fake, "logic_record_status", boom_on_second)
    fake._logic_record_canned_chunk = np.zeros((5, 16), dtype=np.uint8)
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.1)
    stop_result = await logic.record_stop(record_id=rid)
    assert stop_result["error"] is not None


@pytest.mark.asyncio
async def test_record_claims_released_after_stop(logic: Logic) -> None:
    fake: FakeBackend = logic.device.backend  # type: ignore[assignment]
    fake.set_logic_record_status_sequence([(10, 0, 0)])
    start_result = await logic.record_start(
        pins=["dio0"], sample_rate_hz=1_000_000, duration_s=0.01
    )
    rid = start_result["record_id"]
    await asyncio.sleep(0.05)
    await logic.record_stop(record_id=rid)
    assert logic.device.allocator.claimed_pins() == {}
