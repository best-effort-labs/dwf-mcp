"""The synchronous status-poll loops in scope.capture / logic.capture /
dmm.measure must yield the CPU between polls (time.sleep) instead of busy-waiting
and pinning a core at 100% for the whole acquisition.

Each test forces a non-"Done" first poll via the fake's status sequence, then
asserts the loop slept at least once rather than spinning.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.dmm import DMM
from dwf_mcp.instruments.logic import Logic
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.policy import SafetyPolicy


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    dev = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    dev.open()
    return dev


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactWriter:
    return ArtifactWriter(workspace=tmp_path)


@pytest.fixture
def sleep_calls(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda secs: calls.append(secs))
    return calls


def test_scope_capture_yields_between_polls(device, artifacts, sleep_calls) -> None:
    device.backend.set_scope_status_sequence(["Armed", "Done"])  # type: ignore[attr-defined]
    scope = Scope(device=device, artifacts=artifacts)
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=128)
    scope.capture()
    assert sleep_calls, "scope.capture busy-waited without yielding"


def test_logic_capture_yields_between_polls(device, artifacts, sleep_calls) -> None:
    device.backend.set_logic_status_sequence(["Armed", "Done"])  # type: ignore[attr-defined]
    logic = Logic(device=device, artifacts=artifacts)
    logic.configure(pins=["dio0"], sample_rate_hz=1_000_000, buffer_size=128)
    logic.capture()
    assert sleep_calls, "logic.capture busy-waited without yielding"


def test_dmm_measure_yields_between_polls(device, artifacts, sleep_calls) -> None:
    device.backend.set_dmm_status_sequence(["Settling", "Done"])  # type: ignore[attr-defined]
    dmm = DMM(device=device, artifacts=artifacts)
    dmm.measure(channel=1, range_v=5.0, n_averages=4)
    assert sleep_calls, "dmm.measure busy-waited without yielding"
