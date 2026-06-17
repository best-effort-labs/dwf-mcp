from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend, make_dd_device
from dwf_mcp.device import DwfDevice
from dwf_mcp.instruments.dio import DIO
from dwf_mcp.policy import SafetyPolicy


@pytest.fixture
def dd_device(tmp_path: Path) -> DwfDevice:
    d = DwfDevice(
        backend=FakeBackend(devices=[make_dd_device()]),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    d.open(serial="DD-0001")
    return d


@pytest.fixture
def dd_dio(dd_device: DwfDevice, tmp_path: Path) -> DIO:
    return DIO(device=dd_device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_set_drive_records_amp_and_slew(dd_dio: DIO) -> None:
    out = dd_dio.set_drive(milliamps=8.0, slew=1)
    assert out["milliamps"] == 8.0 and out["slew"] == 1
    fake: FakeBackend = dd_dio.device.backend  # type: ignore[assignment]
    assert fake.drive == (0, 0.008, 1)


def test_set_drive_amp_out_of_range_rejected(dd_dio: DIO) -> None:
    with pytest.raises(ValueError, match="2.0.*16.0|range"):
        dd_dio.set_drive(milliamps=50.0, slew=0)
