from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator, PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.dmm import DMM
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
def dmm(device: DwfDevice, tmp_path: Path) -> DMM:
    device.open()
    return DMM(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_measure_calls_backend_sequence(dmm: DMM) -> None:
    result = dmm.measure(channel=1, range_v=5.0)
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    names = [c[0] for c in fake.dmm_calls]
    assert names == ["configure", "arm", "stop"]


def test_measure_returns_statistics(dmm: DMM) -> None:
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    fake.set_dmm_canned_data(1, np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float64))
    result = dmm.measure(channel=1, range_v=5.0, n_averages=4)
    assert result["mean_v"] == pytest.approx(2.5)
    assert result["min_v"] == pytest.approx(1.0)
    assert result["max_v"] == pytest.approx(4.0)
    assert result["channel"] == 1
    assert result["range_v"] == 5.0
    assert result["coupling"] == "DC"


def test_measure_claim_released_after_call(dmm: DMM) -> None:
    dmm.measure(channel=1, range_v=5.0)
    assert dmm.device.allocator.claimed_pins() == {}


def test_measure_claims_both_scope_pins(dmm: DMM) -> None:
    # Intercept after configure to verify claim is held during measurement.
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    claimed: dict = {}

    original_arm = fake.dmm_arm
    def spy_arm() -> None:
        claimed.update(dmm.device.allocator.claimed_pins())
        original_arm()
    fake.dmm_arm = spy_arm  # type: ignore[method-assign]

    dmm.measure(channel=1, range_v=5.0)
    assert "scope1" in claimed
    assert "scope2" in claimed


def test_measure_raises_if_scope_holds_pin(dmm: DMM) -> None:
    dmm.device.allocator.claim("scope", ["scope1"])
    with pytest.raises(PinAllocationError):
        dmm.measure(channel=1, range_v=5.0)


def test_measure_releases_claim_on_backend_exception(dmm: DMM) -> None:
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    def raise_on_arm() -> None:
        raise RuntimeError("backend failed")
    fake.dmm_arm = raise_on_arm  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="backend failed"):
        dmm.measure(channel=1, range_v=5.0)
    assert dmm.device.allocator.claimed_pins() == {}


def test_measure_calls_dmm_stop_on_exception(dmm: DMM) -> None:
    fake: FakeBackend = dmm.device.backend  # type: ignore[assignment]
    def raise_on_arm() -> None:
        raise RuntimeError("backend failed")
    fake.dmm_arm = raise_on_arm  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        dmm.measure(channel=1, range_v=5.0)
    stop_calls = [c for c in fake.dmm_calls if c[0] == "stop"]
    assert len(stop_calls) == 1


def test_measure_invalid_coupling_raises(dmm: DMM) -> None:
    with pytest.raises(ValueError, match="coupling"):
        dmm.measure(channel=1, range_v=5.0, coupling="INVALID")
