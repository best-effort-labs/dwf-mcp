from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator, PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.spi import SPI
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
def spi(device: DwfDevice, tmp_path: Path) -> SPI:
    device.open()
    return SPI(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pins(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3")
    claimed = spi.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"spi_engine", "dio0", "dio1", "dio2", "dio3"}
    assert all(v == "spi" for v in claimed.values())


def test_configure_clk_only(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0)
    claimed = spi.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"spi_engine", "dio0"}


def test_configure_calls_backend(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=500_000, mode=1,
                  mosi_pin="dio1", cs_pin="dio3")
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    cfg = fake.spi_calls[0]
    assert cfg[0] == "configure"
    assert cfg[1]["freq_hz"] == 500_000
    assert cfg[1]["mode"] == 1
    assert cfg[1]["mosi_idx"] == 1
    assert cfg[1]["miso_idx"] is None
    assert cfg[1]["cs_idx"] == 3


def test_configure_releases_on_backend_exception(spi: SPI) -> None:
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.spi_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    assert spi.device.allocator.claimed_pins() == {}
    assert not spi._configured


def test_reconfigure_failed_leaves_unconfigured(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.spi_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        spi.configure(clk_pin="dio0", frequency_hz=2_000_000, mode=0, mosi_pin="dio1")
    assert not spi._configured
    assert spi.device.allocator.claimed_pins() == {}


def test_transfer_full_duplex(spi: SPI) -> None:
    fake: FakeBackend = spi.device.backend  # type: ignore[assignment]
    fake.set_spi_canned_rx(bytes([0xAA, 0xBB]))
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3")
    result = spi.transfer(data=[0x01, 0x02])
    assert result["sent"] == [0x01, 0x02]
    assert result["received"] == [0xAA, 0xBB]


def test_transfer_requires_mosi(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, miso_pin="dio2")
    with pytest.raises(InstrumentNotConfigured, match="mosi_pin"):
        spi.transfer(data=[0x01])


def test_transfer_requires_miso(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    with pytest.raises(InstrumentNotConfigured, match="miso_pin"):
        spi.transfer(data=[0x01])


def test_assert_cs_true_without_cs_pin_raises(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2")
    with pytest.raises(InstrumentNotConfigured, match="cs_pin"):
        spi.transfer(data=[0x01], assert_cs=True)


def test_assert_cs_false_without_cs_pin_ok(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0,
                  mosi_pin="dio1", miso_pin="dio2")
    result = spi.transfer(data=[0x01], assert_cs=False)
    assert "sent" in result


def test_write_requires_mosi(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, miso_pin="dio2")
    with pytest.raises(InstrumentNotConfigured, match="mosi_pin"):
        spi.write(data=[0x01])


def test_read_requires_miso(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    with pytest.raises(InstrumentNotConfigured, match="miso_pin"):
        spi.read(length=2)


def test_unconfigured_raises(spi: SPI) -> None:
    with pytest.raises(InstrumentNotConfigured):
        spi.write(data=[0x01])


def test_release_clears_state(spi: SPI) -> None:
    spi.configure(clk_pin="dio0", frequency_hz=1_000_000, mode=0, mosi_pin="dio1")
    spi.release()
    assert not spi._configured
    assert spi.device.allocator.claimed_pins() == {}
