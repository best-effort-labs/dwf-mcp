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
from dwf_mcp.instruments.sniff import Sniff
from dwf_mcp.policy import SafetyPolicy
from tests.unit.test_spi_decoder import _spi_samples


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


def test_sniff_i2c_nak_on_address_byte(sniff: Sniff) -> None:
    """nak=1 from pydwf means NAK on the 1st transmitted byte (the address);
    we expose it as nak_at_byte=0 (0-based, address counted as byte 0)."""
    import pyarrow.parquet as pq
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_i2c_spy_sequence([
        (1, 1, [0xA0], 1),  # start + addr 0x50w + stop, NAK on address (raw nak=1)
    ])
    result = asyncio.run(sniff.i2c(
        sda_pin="dio0", scl_pin="dio1", duration_s=0.02, poll_interval_s=0.001
    ))
    assert result["count"] == 1
    assert result["error_count"] == 1
    table = pq.read_table(result["artifact_path"])
    assert table.column("nak_at_byte")[0].as_py() == 0
    assert table.column("error")[0].as_py() is True
    assert table.column("error_detail")[0].as_py() == "nak on address byte"


def test_sniff_i2c_nak_on_data_byte(sniff: Sniff) -> None:
    """nak=2 (raw) → NAK on 2nd transmitted byte = first data byte → nak_at_byte=1."""
    import pyarrow.parquet as pq
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_i2c_spy_sequence([
        (1, 1, [0xA0, 0x55], 2),  # start + addr + one data byte + stop; NAK on byte 2
    ])
    result = asyncio.run(sniff.i2c(
        sda_pin="dio0", scl_pin="dio1", duration_s=0.02, poll_interval_s=0.001
    ))
    table = pq.read_table(result["artifact_path"])
    assert table.column("nak_at_byte")[0].as_py() == 1
    assert table.column("error_detail")[0].as_py() == "nak on data byte 0"


def test_sniff_i2c_releases_pins(sniff: Sniff) -> None:
    asyncio.run(sniff.i2c(sda_pin="dio0", scl_pin="dio1", duration_s=0.01))
    assert "sniff_i2c" not in sniff.device.allocator.claimed_instruments()


def test_sniff_i2c_calls_spy_stop_on_completion(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    asyncio.run(sniff.i2c(sda_pin="dio0", scl_pin="dio1", duration_s=0.01))
    spy_calls = [c[0] for c in fake.sniff_calls]
    assert "i2c_spy_start" in spy_calls
    assert "i2c_spy_stop" in spy_calls


# --- sniff.spi_start / spi_status / spi_stop ---


def test_spi_start_returns_sniff_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(10, 0, 1), (0, 0, 0)])

    async def run() -> dict:
        result = await sniff.spi_start(clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000)
        await sniff.spi_stop(result["sniff_id"])
        return result

    result = asyncio.run(run())
    assert "sniff_id" in result
    assert isinstance(result["sniff_id"], str)


def test_release_cancels_active_spi_sessions(sniff: Sniff) -> None:
    """Sniff.release() must cancel background record_loop tasks for in-flight
    SPI sessions so they don't continue polling the backend after release."""
    fake: FakeBackend = sniff.device.backend  # type: ignore
    # Keep record_loop alive by never reporting done.
    fake.set_logic_record_status_sequence([(0, 0, 1)] * 100)

    async def run() -> tuple[asyncio.Task, asyncio.Task | None]:
        start = await sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000,
        )
        session = sniff._spi_sessions[start["sniff_id"]]
        record_task = session.task
        notif_task = session.notification_task
        sniff.release()
        # release() is sync; cancellation propagates on next await.
        await asyncio.sleep(0.05)
        assert record_task is not None
        return record_task, notif_task

    record_task, notif_task = asyncio.run(run())
    assert record_task.done()
    if notif_task is not None:
        assert notif_task.done()
    assert sniff._spi_sessions == {}


def test_spi_status_reports_samples(sniff: Sniff) -> None:
    samples, _ = _spi_samples([0xA5])
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake._logic_record_canned_chunk = samples
    fake.set_logic_record_status_sequence([(len(samples), 0, 1), (0, 0, 0)])

    async def run() -> tuple[dict, dict]:
        start = await sniff.spi_start(clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000)
        await asyncio.sleep(0.05)
        status = sniff.spi_status(start["sniff_id"])
        stop_result = await sniff.spi_stop(start["sniff_id"])
        return status, stop_result

    status, _ = asyncio.run(run())
    assert "samples_received" in status
    assert "lost_samples" in status


def test_spi_status_reports_done(sniff: Sniff) -> None:
    """spi_status returns done=True when the record_loop has finished."""
    fake: FakeBackend = sniff.device.backend  # type: ignore
    samples, _ = _spi_samples([0xA5])
    fake._logic_record_canned_chunk = samples
    # remaining=0 → record_loop sets session.done = True
    fake.set_logic_record_status_sequence([(len(samples), 0, 0)])

    async def run() -> dict:
        start = await sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000,
        )
        await asyncio.sleep(0.05)  # let record_loop poll once
        status = sniff.spi_status(start["sniff_id"])
        await sniff.spi_stop(start["sniff_id"])
        return status

    status = asyncio.run(run())
    assert status["done"] is True
    assert "samples_received" in status
    assert "lost_samples" in status


def test_spi_stop_decodes_and_writes_parquet(sniff: Sniff) -> None:
    samples, _ = _spi_samples([0xA5, 0x5A])
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake._logic_record_canned_chunk = samples
    fake.set_logic_record_status_sequence([(len(samples), 0, 1), (0, 0, 0)])

    async def run() -> dict:
        start = await sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3",
            mode=0, freq_hz=100_000,
        )
        await asyncio.sleep(0.05)
        return await sniff.spi_stop(start["sniff_id"])

    result = asyncio.run(run())
    assert result["artifact_error"] is None, f"decode error: {result['artifact_error']}"
    assert result["artifact_path"] is not None
    assert result["count"] >= 2


def test_spi_stop_pin_map_uses_dio_number_not_list_index(sniff: Sniff) -> None:
    """Regression: pin_map must use the DIO number (e.g. 4) not the list index (e.g. 0).

    Use non-zero-based pins dio4/dio5/dio6/dio7 so that list-index (0,1,2,3) and
    DIO number (4,5,6,7) differ.  The sample array is built with signals in columns
    4,5,6,7 to match the expected DIO numbers.
    """
    # Build base samples with signals in columns 0,1,2,3 (16 columns wide)
    base_samples, _ = _spi_samples([0xA5, 0x5A])  # shape (N, 16)

    # Shift signals from columns 0-3 → 4-7 by rearranging columns
    shifted = np.zeros_like(base_samples)
    shifted[:, 4] = base_samples[:, 0]  # CLK   → col 4 (dio4)
    shifted[:, 5] = base_samples[:, 1]  # MOSI  → col 5 (dio5)
    shifted[:, 6] = base_samples[:, 2]  # MISO  → col 6 (dio6)
    shifted[:, 7] = base_samples[:, 3]  # CS    → col 7 (dio7)

    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake._logic_record_canned_chunk = shifted
    fake.set_logic_record_status_sequence([(len(shifted), 0, 1), (0, 0, 0)])

    async def run() -> dict:
        start = await sniff.spi_start(
            clk_pin="dio4", mosi_pin="dio5", miso_pin="dio6", cs_pin="dio7",
            mode=0, freq_hz=100_000,
        )
        await asyncio.sleep(0.05)
        return await sniff.spi_stop(start["sniff_id"])

    result = asyncio.run(run())
    assert result["artifact_error"] is None, f"decode error: {result['artifact_error']}"
    assert result["artifact_path"] is not None
    assert result["count"] >= 2, (
        "Expected ≥2 decoded words; wrong pin_map (list-index vs DIO number) would produce 0"
    )


def test_spi_stop_releases_observer_claim(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 0)])

    async def run() -> None:
        start = await sniff.spi_start(clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000)
        await sniff.spi_stop(start["sniff_id"])

    asyncio.run(run())
    # After stop, DigitalIn observer is released — can claim logic again
    assert len(sniff.device.allocator._observe_claims) == 0


# --- sniff.i2c_start / i2c_status / i2c_stop (async observe-mode) ---


def test_sniff_i2c_start_returns_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> dict:
        r = await sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.1,
        )
        await sniff.i2c_stop(r["sniff_id"])
        return r

    result = asyncio.run(run())
    assert "sniff_id" in result
    assert isinstance(result["sniff_id"], str)


def test_sniff_i2c_start_memory_cap_raises(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="32 MB"):
        asyncio.run(sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000,
            max_duration_s=3600, sample_rate_hz=100e6,
        ))


def test_sniff_i2c_start_oversampling_rejected(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="oversampling"):
        asyncio.run(sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000,
            max_duration_s=0.1, sample_rate_hz=200_000,  # 2x — below 4x floor
        ))


def test_sniff_i2c_does_not_claim_engine_or_dio(sniff: Sniff) -> None:
    """observe-mode must not block a concurrent i2c master on the same wires."""
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> None:
        r = await sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.1,
        )
        # A separate instrument MUST be able to claim i2c_engine + the same DIO pins.
        sniff.device.allocator.claim("i2c_master", ["i2c_engine", "dio0", "dio1"])
        await sniff.i2c_stop(r["sniff_id"])

    asyncio.run(run())


def test_sniff_i2c_status_reports_done(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 0)])  # done=True on first poll

    async def run() -> dict:
        r = await sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.1,
        )
        await asyncio.sleep(0.05)
        status = sniff.i2c_status(r["sniff_id"])
        await sniff.i2c_stop(r["sniff_id"])
        return status

    status = asyncio.run(run())
    assert status["done"] is True
