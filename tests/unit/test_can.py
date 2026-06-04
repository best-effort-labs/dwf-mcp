from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.can import CAN
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
def can(device: DwfDevice, tmp_path: Path) -> CAN:
    device.open()
    return CAN(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_both_pins(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    claimed = can.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"dio0", "dio1"}


def test_configure_calls_backend(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=250_000)
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    cfg = fake.can_calls[0]
    assert cfg[0] == "configure"
    assert cfg[1]["tx_idx"] == 0
    assert cfg[1]["rx_idx"] == 1
    assert cfg[1]["bit_rate"] == 250_000


def test_configure_releases_on_exception(can: CAN) -> None:
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.can_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    assert can.device.allocator.claimed_pins() == {}


def test_reconfigure_failed_leaves_unconfigured(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.can_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        can.configure(tx_pin="dio2", rx_pin="dio3", bit_rate=250_000)
    assert not can._configured
    assert can.device.allocator.claimed_pins() == {}


def test_send_standard_frame(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.send(id=0x123, data=[0x01, 0x02, 0x03])
    assert result == {"sent": True}
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    sends = [c for c in fake.can_calls if c[0] == "send"]
    assert sends[0][1]["id"] == 0x123
    assert sends[0][1]["data"] == b"\x01\x02\x03"
    assert sends[0][1]["extended"] is False


def test_send_extended_frame(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.send(id=0x12345678, data=[0xFF], extended=True)
    assert result == {"sent": True}
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    sends = [c for c in fake.can_calls if c[0] == "send"]
    assert sends[0][1]["extended"] is True


def test_send_standard_id_too_large_raises(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    with pytest.raises(ValueError, match="0x7FF"):
        can.send(id=0x800, data=[], extended=False)


def test_receive_frame(can: CAN) -> None:
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    fake.set_can_canned_frame(id=0x456, data=b"\xDE\xAD", extended=False, error_count=0)
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.receive()
    assert result["id"] == 0x456
    assert result["data"] == [0xDE, 0xAD]
    assert result["data_hex"] == "dead"
    assert result["extended"] is False
    assert result["error_count"] == 0


def test_receive_timeout_returns_none_id(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.receive(timeout_s=0.1)
    assert result["id"] is None
    assert result["data"] == []
    assert result["error_count"] == 0


def test_receive_propagates_error_count(can: CAN) -> None:
    fake: FakeBackend = can.device.backend  # type: ignore[assignment]
    fake.set_can_canned_frame(id=0x1, data=b"\x00", extended=False, error_count=5)
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    result = can.receive()
    assert result["error_count"] == 5


def test_unconfigured_raises(can: CAN) -> None:
    with pytest.raises(InstrumentNotConfigured):
        can.send(id=0x1, data=[])


def test_release_clears_state(can: CAN) -> None:
    can.configure(tx_pin="dio0", rx_pin="dio1", bit_rate=500_000)
    can.release()
    assert not can._configured
    assert can.device.allocator.claimed_pins() == {}
