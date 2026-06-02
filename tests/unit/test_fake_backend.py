from __future__ import annotations

import pytest

from dwf_mcp.backend import DeviceInfo, DwfBackendError
from dwf_mcp.backends.fake import FakeBackend


def test_enumerate_finds_fake_device() -> None:
    b = FakeBackend()
    devices = b.enumerate()
    assert len(devices) == 1
    assert devices[0].serial == "FAKE-AD3-0001"
    assert devices[0].model == "Analog Discovery 3"


def test_open_close_lifecycle() -> None:
    b = FakeBackend()
    assert not b.is_open
    info = b.open()
    assert b.is_open
    assert isinstance(info, DeviceInfo)
    b.close()
    assert not b.is_open


def test_double_open_returns_same_info() -> None:
    b = FakeBackend()
    info1 = b.open()
    info2 = b.open()
    assert info1 == info2


def test_open_by_serial_matching() -> None:
    b = FakeBackend()
    b.open(serial="FAKE-AD3-0001")
    b.close()
    with pytest.raises(DwfBackendError):
        b.open(serial="DOES-NOT-EXIST")


def test_simulate_unplug() -> None:
    b = FakeBackend()
    b.open()
    b.simulate_unplug()
    assert not b.is_open
