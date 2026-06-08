from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.sniff import Sniff
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
def sniff(device: DwfDevice, tmp_path: Path) -> Sniff:
    device.open()
    return Sniff(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


# --- sniff.uart ---

def test_sniff_uart_calls_backend(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_uart_sniff_frames([(0.001, b"\x41", False), (0.002, b"\x42", False)])

    result = asyncio.run(sniff.uart(rx_pin="dio0", baud=9600, duration_s=0.01))

    assert result["count"] == 2
    assert result["error_count"] == 0
    assert result["artifact_path"] is not None
    calls = [c[0] for c in fake.sniff_calls]
    assert "uart_sniff" in calls


def test_sniff_uart_parity_errors(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_uart_sniff_frames([(0.001, b"\xFF", True)])

    result = asyncio.run(sniff.uart(rx_pin="dio0", baud=9600, duration_s=0.01))
    assert result["error_count"] == 1


def test_sniff_uart_releases_pins(sniff: Sniff) -> None:
    asyncio.run(sniff.uart(rx_pin="dio1", baud=115200, duration_s=0.01))
    assert "sniff_uart" not in sniff.device.allocator.claimed_instruments()


def test_sniff_uart_empty_returns_zero_count(sniff: Sniff) -> None:
    result = asyncio.run(sniff.uart(rx_pin="dio0", baud=9600, duration_s=0.01))
    assert result["count"] == 0
    assert result["artifact_path"] is not None


# --- sniff.can ---

def test_sniff_can_calls_backend(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_can_sniff_frames([(0.001, 0x123, b"\x01\x02", False, 0)])

    result = asyncio.run(sniff.can(rx_pin="dio0", bitrate=500_000, duration_s=0.01))
    assert result["count"] == 1
    assert result["artifact_path"] is not None


def test_sniff_can_releases_pins(sniff: Sniff) -> None:
    asyncio.run(sniff.can(rx_pin="dio0", bitrate=500_000, duration_s=0.01))
    assert "sniff_can" not in sniff.device.allocator.claimed_instruments()


# --- sniff.i2c ---

def test_sniff_i2c_assembles_write_transaction(sniff: Sniff) -> None:
    import pyarrow.parquet as pq
    fake: FakeBackend = sniff.device.backend  # type: ignore
    # addr_byte=0xA0 → address=0x50, direction="write"
    # start=1 with data, then stop=1
    fake.set_i2c_spy_sequence([
        (1, 0, [0xA0, 0x01], 0),  # start=1, data=[addr, data_byte]
        (0, 1, [], 0),             # stop=1
    ])

    result = asyncio.run(sniff.i2c(
        sda_pin="dio0", scl_pin="dio1", duration_s=0.02, poll_interval_s=0.001
    ))

    assert result["count"] == 1
    assert result["artifact_path"] is not None
    table = pq.read_table(result["artifact_path"])
    assert table.num_rows == 1
    assert table.column("type")[0].as_py() == "write"
    assert table.column("address")[0].as_py() == 0x50


def test_sniff_i2c_releases_pins(sniff: Sniff) -> None:
    asyncio.run(sniff.i2c(sda_pin="dio0", scl_pin="dio1", duration_s=0.01))
    assert "sniff_i2c" not in sniff.device.allocator.claimed_instruments()


def test_sniff_i2c_calls_spy_stop_on_completion(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    asyncio.run(sniff.i2c(sda_pin="dio0", scl_pin="dio1", duration_s=0.01))
    spy_calls = [c[0] for c in fake.sniff_calls]
    assert "i2c_spy_start" in spy_calls
    assert "i2c_spy_stop" in spy_calls
