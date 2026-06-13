"""Tests for Scope record_start/status/stop and _mode state machine."""
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
from dwf_mcp.instruments.scope import Scope
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
def scope(device: DwfDevice, tmp_path: Path) -> Scope:
    device.open()
    return Scope(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


@pytest.mark.asyncio
async def test_record_start_returns_record_id(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert "record_id" in result
    assert isinstance(result["record_id"], str)


@pytest.mark.asyncio
async def test_record_start_sets_mode_to_record(scope: Scope) -> None:
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert scope._mode == "record"


@pytest.mark.asyncio
async def test_record_start_claims_scope_pins(scope: Scope) -> None:
    await scope.record_start(
        channels=[1, 2], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    claimed = scope.device.allocator.claimed_pins()
    assert "scope1" in claimed and "scope2" in claimed


@pytest.mark.asyncio
async def test_configure_while_in_record_mode_raises(scope: Scope) -> None:
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    with pytest.raises(RuntimeError, match="record mode"):
        scope.configure(channels=[1], range_v=5.0, sample_rate_hz=10_000.0, buffer_size=1024)


@pytest.mark.asyncio
async def test_record_start_while_in_record_mode_raises(scope: Scope) -> None:
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    with pytest.raises(RuntimeError, match="record mode"):
        await scope.record_start(
            channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
        )


def test_configure_sets_mode_to_buffer(scope: Scope) -> None:
    scope.configure(channels=[1], range_v=5.0, sample_rate_hz=10_000.0, buffer_size=1024)
    assert scope._mode == "buffer"


@pytest.mark.asyncio
async def test_record_start_while_in_buffer_mode_releases_buffer(scope: Scope) -> None:
    scope.configure(channels=[1], range_v=5.0, sample_rate_hz=10_000.0, buffer_size=1024)
    assert scope._mode == "buffer"
    await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert scope._mode == "record"
    assert scope._config is None


@pytest.mark.asyncio
async def test_record_status_returns_fields(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    status = scope.record_status(record_id)
    assert status["record_id"] == record_id
    assert "done" in status
    assert "lost_samples" in status


@pytest.mark.asyncio
async def test_record_stop_writes_npz_artifact(scope: Scope, tmp_path: Path) -> None:
    fake: FakeBackend = scope.device.backend  # type: ignore[assignment]
    fake._scope_record_canned_chunk = np.random.rand(10, 2).astype(np.float64)
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    stop = await scope.record_stop(record_id)
    assert stop["artifact_error"] is None
    assert stop["artifact_path"] is not None
    assert Path(stop["artifact_path"]).exists()


@pytest.mark.asyncio
async def test_record_stop_releases_pins(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    await scope.record_stop(record_id)
    claimed = scope.device.allocator.claimed_pins()
    assert "scope1" not in claimed


@pytest.mark.asyncio
async def test_record_stop_resets_mode_to_none(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    await scope.record_stop(record_id)
    assert scope._mode is None


@pytest.mark.asyncio
async def test_record_stop_unknown_id_raises(scope: Scope) -> None:
    with pytest.raises(ValueError, match="unknown record_id"):
        await scope.record_stop("no-such-id")


@pytest.mark.asyncio
async def test_record_after_stop_is_possible(scope: Scope) -> None:
    result = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    await asyncio.sleep(0.05)
    await scope.record_stop(result["record_id"])
    # Can start another record after stopping
    result2 = await scope.record_start(
        channels=[1], range_v=5.0, sample_rate_hz=10_000.0, duration_s=0.01
    )
    assert "record_id" in result2
