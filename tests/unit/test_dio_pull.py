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


def test_set_pull_dio_per_pin_rmw(dd_dio: DIO) -> None:
    dd_dio.set_pull("dio24", "up")
    dd_dio.set_pull("dio25", "down")
    be: FakeBackend = dd_dio.device.backend  # type: ignore[assignment]
    assert be.pull_up_mask & 0b1       # dio24 -> bit0 up
    assert be.pull_down_mask & 0b10    # dio25 -> bit1 down


def test_set_pull_din_is_bank_global(dd_dio: DIO) -> None:
    out = dd_dio.set_pull("din5", "up")
    assert out["scope"] == "din_bank"


def test_set_pull_none_clears(dd_dio: DIO) -> None:
    dd_dio.set_pull("dio24", "up")
    dd_dio.set_pull("dio24", "none")
    be: FakeBackend = dd_dio.device.backend  # type: ignore[assignment]
    assert not (be.pull_up_mask & 0b1)


def test_set_pull_returns_pin_scope_for_dio(dd_dio: DIO) -> None:
    result = dd_dio.set_pull("dio24", "up")
    assert result["scope"] == "pin"
    assert result["pin"] == "dio24"
    assert result["mode"] == "up"


def test_set_pull_not_supported_raises(tmp_path: Path) -> None:
    """set_pull raises ValueError on a device that does not support pull."""
    from dwf_mcp.backends.fake import make_fake_device
    device = DwfDevice(
        backend=FakeBackend(devices=[make_fake_device()]),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.open()
    dio = DIO(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
    with pytest.raises(ValueError, match="pull not supported"):
        dio.set_pull("dio0", "up")


def test_set_pull_rmw_preserves_other_bits(dd_dio: DIO) -> None:
    """Setting pull on dio25 must not disturb the dio24 state already set."""
    dd_dio.set_pull("dio24", "up")
    dd_dio.set_pull("dio25", "up")
    be: FakeBackend = dd_dio.device.backend  # type: ignore[assignment]
    assert be.pull_up_mask & 0b1   # dio24 still up
    assert be.pull_up_mask & 0b10  # dio25 up too


def test_set_pull_din_stores_mode(dd_dio: DIO) -> None:
    dd_dio.set_pull("din5", "down")
    be: FakeBackend = dd_dio.device.backend  # type: ignore[assignment]
    assert be.din_pull == "down"


def test_set_pull_keeper_sets_both_masks(dd_dio: DIO) -> None:
    """Keeper (bus-hold) asserts both pull-up and pull-down on the pin's bit."""
    out = dd_dio.set_pull("dio24", "keeper")
    be: FakeBackend = dd_dio.device.backend  # type: ignore[assignment]
    assert be.pull_up_mask & 0b1
    assert be.pull_down_mask & 0b1
    assert out["mode"] == "keeper" and out["scope"] == "pin"


def test_set_pull_none_clears_keeper(dd_dio: DIO) -> None:
    dd_dio.set_pull("dio24", "keeper")
    dd_dio.set_pull("dio24", "none")
    be: FakeBackend = dd_dio.device.backend  # type: ignore[assignment]
    assert not (be.pull_up_mask & 0b1)
    assert not (be.pull_down_mask & 0b1)


def test_set_pull_keeper_rejected_on_din_bank(dd_dio: DIO) -> None:
    """The DIN bank pull is the DINPP scalar (down/none/up) — keeper isn't available."""
    with pytest.raises(ValueError, match="keeper.*not supported.*DIN"):
        dd_dio.set_pull("din5", "keeper")


# --- bank-global pull (ADP2230): one pull setting for the whole DIO bank ---

@pytest.fixture
def bank_global_dio(tmp_path: Path) -> DIO:
    from dwf_mcp.backend import DeviceInfo
    info = DeviceInfo(
        serial="ADP", model="Analog Discovery Pro 2230", firmware="x",
        sample_rate_max_hz=1e8, dio_count=16, analog_in_channels=2,
        analog_out_channels=3, devid=14,
        dio_pull_supported=True, dio_pull_bank_global=True,
    )
    d = DwfDevice(
        backend=FakeBackend(devices=[info]), policy=SafetyPolicy(),
        allocator=PinAllocator(), workspace=tmp_path, idle_timeout_s=60,
    )
    d.open(serial="ADP")
    return DIO(device=d, artifacts=ArtifactWriter(workspace=tmp_path))


def test_bank_global_sets_whole_bank_and_reports_scope(bank_global_dio: DIO) -> None:
    out = bank_global_dio.set_pull("dio0", "up")
    be: FakeBackend = bank_global_dio.device.backend  # type: ignore[assignment]
    assert be.pull_up_mask == 0xFFFF and be.pull_down_mask == 0
    assert out["scope"] == "bank"


def test_bank_global_switch_does_not_accumulate(bank_global_dio: DIO) -> None:
    """The bug this fixes: up then down must NOT leave the up still set bank-wide."""
    bank_global_dio.set_pull("dio0", "up")
    bank_global_dio.set_pull("dio1", "down")
    be: FakeBackend = bank_global_dio.device.backend  # type: ignore[assignment]
    assert be.pull_up_mask == 0 and be.pull_down_mask == 0xFFFF


def test_bank_global_keeper_and_none(bank_global_dio: DIO) -> None:
    bank_global_dio.set_pull("dio0", "keeper")
    be: FakeBackend = bank_global_dio.device.backend  # type: ignore[assignment]
    assert be.pull_up_mask == 0xFFFF and be.pull_down_mask == 0xFFFF
    bank_global_dio.set_pull("dio0", "none")
    assert be.pull_up_mask == 0 and be.pull_down_mask == 0
