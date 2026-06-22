from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocationError, PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.i2c import I2C
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
def i2c(device: DwfDevice, tmp_path: Path) -> I2C:
    device.open()
    return I2C(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_dio_pins(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    assert i2c.device.allocator.claimed_pins() == {
        "i2c_engine": "i2c", "dio0": "i2c", "dio1": "i2c"}


def test_configure_rejects_conflicting_pins(i2c: I2C) -> None:
    i2c.device.allocator.claim("uart", ["dio0"])
    with pytest.raises(PinAllocationError):
        i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)


def test_reconfigure_swaps_pins(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    i2c.configure(sda_pin="dio4", scl_pin="dio5", clock_hz=400_000)
    assert i2c.device.allocator.claimed_pins() == {
        "i2c_engine": "i2c", "dio4": "i2c", "dio5": "i2c"}


def test_write_without_configure_raises(i2c: I2C) -> None:
    with pytest.raises(InstrumentNotConfigured):
        i2c.write(address=0x50, data=b"\x00")


def test_write_returns_ack_status(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_acks({0x50: True})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.write(address=0x50, data=b"\x00\x01")
    assert result == {"address": 0x50, "ack": True, "nak_count": 0}


def test_read_returns_bytes_hex(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_reads({0x50: b"\xde\xad"})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.read(address=0x50, length=2)
    assert result == {"address": 0x50, "data_hex": "dead", "data": [0xde, 0xad]}


def test_write_read_combined(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_reads({0x50: b"\x01\x02\x03"})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.write_read(address=0x50, write=[0x10], read_length=3)
    assert result["data_hex"] == "010203"


def test_scan_returns_acked_addresses(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_acks({0x20: True, 0x50: True})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.scan()
    assert result["found"] == [0x20, 0x50]
    assert result["count"] == 2


def test_release_clears_pins(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    i2c.release()
    assert i2c.device.allocator.claimed_pins() == {}


def test_pullups_kept_for_sidecar(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, pullups=True)
    assert i2c._pullups is True  # type: ignore[attr-defined]


def test_engine_conflict_blocks_second_instrument(i2c: I2C) -> None:
    """Once i2c claims the i2c_engine virtual pin, no other instrument may claim it."""
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    with pytest.raises(PinAllocationError):
        i2c.device.allocator.claim("another_instrument", ["i2c_engine", "dio5", "dio6"])


def test_configure_backend_failure_releases_pins(i2c: I2C, monkeypatch) -> None:
    """If a backend call raises mid-configure, the pin claim must be rolled back
    and the instrument must remain unconfigured.

    Tests the partial-failure pattern from Scope/Supply, now in I2C.
    """
    backend = i2c.device.backend
    def boom_i2c_configure(**kwargs):
        raise RuntimeError("backend on fire")
    monkeypatch.setattr(backend, "i2c_configure", boom_i2c_configure)
    with pytest.raises(RuntimeError):
        i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    assert i2c.device.allocator.claimed_pins() == {}
    assert i2c._configured is False  # noqa: SLF001
