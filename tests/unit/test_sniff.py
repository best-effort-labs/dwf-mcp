from __future__ import annotations

import asyncio
import tracemalloc
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.sniff import Sniff
from dwf_mcp.policy import SafetyPolicy
from tests.unit.test_can_decoder import _can_bits, _samples_from_bits, _stuff
from tests.unit.test_i2c_decoder import _i2c_samples
from tests.unit.test_spi_decoder import _spi_samples
from tests.unit.test_uart_decoder import _uart_samples


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
        result = await sniff.spi_start(clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000, max_duration_s=0.1)
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
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000, max_duration_s=0.1,
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
        start = await sniff.spi_start(clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000, max_duration_s=0.1)
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
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000, max_duration_s=0.1,
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
            mode=0, freq_hz=100_000, max_duration_s=0.1,
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
            mode=0, freq_hz=100_000, max_duration_s=0.1,
        )
        await asyncio.sleep(0.05)
        return await sniff.spi_stop(start["sniff_id"])

    result = asyncio.run(run())
    assert result["artifact_error"] is None, f"decode error: {result['artifact_error']}"
    assert result["artifact_path"] is not None
    assert result["count"] >= 2, (
        "Expected ≥2 decoded words; wrong pin_map (list-index vs DIO number) would produce 0"
    )


def test_spi_start_memory_cap_raises(sniff: Sniff) -> None:
    """sniff.spi_start must enforce the 32 MB raw memory cap like the other
    async sniff tools."""
    # freq_hz=10e6 → sample_rate=100MHz, 2 pins, 3600s → ~720 GB → way over.
    with pytest.raises(ValueError, match="32 MB"):
        asyncio.run(sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0,
            freq_hz=10_000_000, max_duration_s=3600.0,
        ))


def test_spi_stop_releases_observer_claim(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 0)])

    async def run() -> None:
        start = await sniff.spi_start(clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000, max_duration_s=0.1)
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


# --- sniff.uart_start / uart_status / uart_stop (async observe-mode) ---


def test_sniff_uart_start_returns_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> dict:
        r = await sniff.uart_start(rx_pin="dio0", baud=9600, max_duration_s=0.1)
        await sniff.uart_stop(r["sniff_id"])
        return r

    result = asyncio.run(run())
    assert "sniff_id" in result
    assert isinstance(result["sniff_id"], str)


def test_sniff_uart_memory_cap_raises(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="32 MB"):
        asyncio.run(sniff.uart_start(
            rx_pin="dio0", baud=9600, max_duration_s=3600, sample_rate_hz=100e6,
        ))


def test_sniff_uart_oversampling_rejected(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="oversampling"):
        asyncio.run(sniff.uart_start(
            rx_pin="dio0", baud=9600, max_duration_s=0.1, sample_rate_hz=20_000,  # 2.08x
        ))


def test_sniff_uart_does_not_claim_uart_engine_or_dio(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> None:
        r = await sniff.uart_start(rx_pin="dio0", baud=9600, max_duration_s=0.1)
        # Other instruments can claim uart_engine + dio0 while sniff observes.
        sniff.device.allocator.claim("uart_master", ["uart_engine", "dio0"])
        await sniff.uart_stop(r["sniff_id"])

    asyncio.run(run())


# --- sniff.can_start / can_status / can_stop (async observe-mode) ---


def test_sniff_can_start_returns_id(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> dict:
        r = await sniff.can_start(rx_pin="dio0", bitrate=125_000, max_duration_s=0.1)
        await sniff.can_stop(r["sniff_id"])
        return r

    result = asyncio.run(run())
    assert "sniff_id" in result
    assert isinstance(result["sniff_id"], str)


def test_sniff_can_default_sample_rate_is_20x_bitrate(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> None:
        r = await sniff.can_start(rx_pin="dio0", bitrate=100_000, max_duration_s=0.05)
        session = sniff._async_sessions[r["sniff_id"]]
        assert session.meta["sample_rate_hz"] == 2_000_000  # 20x
        await sniff.can_stop(r["sniff_id"])

    asyncio.run(run())


def test_sniff_can_memory_cap_raises(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="32 MB"):
        asyncio.run(sniff.can_start(
            rx_pin="dio0", bitrate=125_000, max_duration_s=3600, sample_rate_hz=100e6,
        ))


def test_sniff_can_oversampling_rejected(sniff: Sniff) -> None:
    with pytest.raises(ValueError, match="oversampling"):
        asyncio.run(sniff.can_start(
            rx_pin="dio0", bitrate=125_000, max_duration_s=0.1,
            sample_rate_hz=500_000,  # 4x — below 8x floor for CAN
        ))


def test_sniff_can_does_not_claim_can_engine_or_dio(sniff: Sniff) -> None:
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> None:
        r = await sniff.can_start(rx_pin="dio0", bitrate=125_000, max_duration_s=0.1)
        sniff.device.allocator.claim("can_master", ["can_engine", "dio0"])
        await sniff.can_stop(r["sniff_id"])

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Step 1: Per-protocol stream-path correctness (4 tests)
# ---------------------------------------------------------------------------


def _make_fake_backend_with_chunks(
    device: DwfDevice,
    chunks: list[np.ndarray],
) -> None:
    """Configure a fresh FakeBackend on *device* with the given chunks.

    Each chunk produces one poll return of (len(chunk), 0, remaining).
    After all chunks, one final poll returns (0, 0, 0) to signal done.
    """
    fake: FakeBackend = device.backend  # type: ignore
    status_seq = [(len(c), 0, 1) for c in chunks]
    status_seq.append((0, 0, 0))
    fake.set_logic_record_status_sequence(status_seq)
    fake.set_logic_record_chunks(list(chunks))


def _reset_device(tmp_path: Path) -> DwfDevice:
    """Return a fresh DwfDevice with a fresh FakeBackend."""
    dev = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    dev.open()
    return dev


def test_sniff_i2c_stream_decode_matches_accumulation(
    tmp_path: Path,
) -> None:
    """Streaming and accumulation modes must produce identical I2C transactions.

    The transaction boundaries are set so the STOP condition of the first
    transaction falls in the second chunk, exercising the cross-boundary path.
    """
    # Build samples for 2 I2C write transactions using the shared helper.
    # sample_rate_hz=1e6, clock_hz=100kHz → 10 samples/bit → ~320 samples/transaction.
    all_samples = _i2c_samples(
        [(0x50, b"\xAB", True), (0x60, b"\xCD", True)],
        sample_rate_hz=1_000_000.0,
        clock_hz=100_000.0,
    )
    # Split so the boundary falls in the middle — first transaction's STOP is in chunk 1.
    n = all_samples.shape[0]
    cut = n // 3  # first third: contains START + address byte only
    chunk0 = all_samples[:cut]
    chunk1 = all_samples[cut:]

    # --- accumulation mode ---
    dev_acc = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_acc, [chunk0, chunk1])
    sniff_acc = Sniff(device=dev_acc, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_accum() -> list[Any]:
        r = await sniff_acc.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.5,
            sample_rate_hz=1_000_000.0,
        )
        await asyncio.sleep(0.1)
        result = await sniff_acc.i2c_stop(r["sniff_id"])
        return result  # type: ignore[return-value]

    acc_result = asyncio.run(run_accum())
    txns_accumulation = acc_result["count"]

    # --- stream mode ---
    dev_str = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_str, [chunk0, chunk1])
    sniff_str = Sniff(device=dev_str, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_stream() -> list[Any]:
        r = await sniff_str.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.5,
            sample_rate_hz=1_000_000.0, stream_decode=True,
        )
        await asyncio.sleep(0.1)
        return await sniff_str.i2c_stop(r["sniff_id"])

    str_result = asyncio.run(run_stream())
    txns_streaming = str_result["count"]

    assert txns_accumulation >= 1, "expected at least 1 I2C transaction from synthetic samples"
    assert txns_streaming == txns_accumulation, (
        f"stream mode produced {txns_streaming} transactions, "
        f"accumulation produced {txns_accumulation}"
    )


def test_sniff_spi_stream_decode_matches_accumulation(
    tmp_path: Path,
) -> None:
    """SPI: streaming and accumulation modes must decode the same transactions.

    The split point is mid-frame so the CS→idle edge (end of word) appears
    in the second chunk.
    """
    all_samples, _ = _spi_samples([0xA5, 0x5A])
    n = all_samples.shape[0]
    cut = n // 3
    chunk0 = all_samples[:cut]
    chunk1 = all_samples[cut:]

    # --- accumulation mode ---
    dev_acc = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_acc, [chunk0, chunk1])
    sniff_acc = Sniff(device=dev_acc, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_accum() -> dict[str, Any]:
        r = await sniff_acc.spi_start(
            clk_pin="dio0", mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3",
            mode=0, freq_hz=100_000, max_duration_s=0.5,
        )
        await asyncio.sleep(0.1)
        return await sniff_acc.spi_stop(r["sniff_id"])

    acc_result = asyncio.run(run_accum())

    # --- stream mode ---
    dev_str = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_str, [chunk0, chunk1])
    sniff_str = Sniff(device=dev_str, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_stream() -> dict[str, Any]:
        r = await sniff_str.spi_start(
            clk_pin="dio0", mosi_pin="dio1", miso_pin="dio2", cs_pin="dio3",
            mode=0, freq_hz=100_000, max_duration_s=0.5, stream_decode=True,
        )
        await asyncio.sleep(0.1)
        return await sniff_str.spi_stop(r["sniff_id"])

    str_result = asyncio.run(run_stream())

    assert acc_result["count"] >= 1, "expected >=1 SPI word from synthetic samples"
    assert str_result["count"] == acc_result["count"], (
        f"SPI stream={str_result['count']} acc={acc_result['count']}"
    )


def test_sniff_uart_stream_decode_matches_accumulation(
    tmp_path: Path,
) -> None:
    """UART: streaming and accumulation modes must decode the same frames.

    Split point: between start-bit and mid-frame so the stop bit of the first
    byte appears in the second chunk.
    """
    all_samples = _uart_samples(b"Hi", baud=9600, sample_rate_hz=96_000.0)
    n = all_samples.shape[0]
    cut = n // 3
    chunk0 = all_samples[:cut]
    chunk1 = all_samples[cut:]

    # --- accumulation mode ---
    dev_acc = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_acc, [chunk0, chunk1])
    sniff_acc = Sniff(device=dev_acc, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_accum() -> dict[str, Any]:
        r = await sniff_acc.uart_start(
            rx_pin="dio0", baud=9600, max_duration_s=0.5,
            sample_rate_hz=96_000.0,
        )
        await asyncio.sleep(0.1)
        return await sniff_acc.uart_stop(r["sniff_id"])

    acc_result = asyncio.run(run_accum())

    # --- stream mode ---
    dev_str = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_str, [chunk0, chunk1])
    sniff_str = Sniff(device=dev_str, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_stream() -> dict[str, Any]:
        r = await sniff_str.uart_start(
            rx_pin="dio0", baud=9600, max_duration_s=0.5,
            sample_rate_hz=96_000.0, stream_decode=True,
        )
        await asyncio.sleep(0.1)
        return await sniff_str.uart_stop(r["sniff_id"])

    str_result = asyncio.run(run_stream())

    assert acc_result["count"] >= 1, "expected >=1 UART frame from synthetic samples"
    assert str_result["count"] == acc_result["count"], (
        f"UART stream={str_result['count']} acc={acc_result['count']}"
    )


def test_sniff_can_stream_decode_matches_accumulation(
    tmp_path: Path,
) -> None:
    """CAN: streaming and accumulation modes must decode the same frames.

    Split mid-frame so the CRC/EOF of the first frame appears in chunk 1.
    """
    bits = _stuff(_can_bits(0x123, b"\xDE\xAD"))
    all_samples = _samples_from_bits(bits, bitrate=100_000, sample_rate_hz=2_000_000.0)
    n = all_samples.shape[0]
    cut = n // 3
    chunk0 = all_samples[:cut]
    chunk1 = all_samples[cut:]

    # --- accumulation mode ---
    dev_acc = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_acc, [chunk0, chunk1])
    sniff_acc = Sniff(device=dev_acc, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_accum() -> dict[str, Any]:
        r = await sniff_acc.can_start(
            rx_pin="dio0", bitrate=100_000, max_duration_s=0.5,
            sample_rate_hz=2_000_000.0,
        )
        await asyncio.sleep(0.1)
        return await sniff_acc.can_stop(r["sniff_id"])

    acc_result = asyncio.run(run_accum())

    # --- stream mode ---
    dev_str = _reset_device(tmp_path)
    _make_fake_backend_with_chunks(dev_str, [chunk0, chunk1])
    sniff_str = Sniff(device=dev_str, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run_stream() -> dict[str, Any]:
        r = await sniff_str.can_start(
            rx_pin="dio0", bitrate=100_000, max_duration_s=0.5,
            sample_rate_hz=2_000_000.0, stream_decode=True,
        )
        await asyncio.sleep(0.1)
        return await sniff_str.can_stop(r["sniff_id"])

    str_result = asyncio.run(run_stream())

    assert acc_result["count"] >= 1, "expected >=1 CAN frame from synthetic samples"
    assert str_result["count"] == acc_result["count"], (
        f"CAN stream={str_result['count']} acc={acc_result['count']}"
    )


# ---------------------------------------------------------------------------
# Step 2: Cap-skip behavior (2 tests)
# ---------------------------------------------------------------------------


def test_sniff_i2c_start_enforces_cap_when_not_streaming(sniff: Sniff) -> None:
    """Without stream_decode, i2c_start must raise ValueError when the projected
    memory exceeds 32 MB. 100 MHz × 3600 s × 2 pins >> 32 MB."""
    with pytest.raises(ValueError, match="exceeds 32 MB cap"):
        asyncio.run(sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000,
            max_duration_s=3600, sample_rate_hz=100_000_000.0,
        ))


def test_sniff_i2c_start_skips_cap_when_streaming(sniff: Sniff) -> None:
    """With stream_decode=True, i2c_start must bypass the memory cap and return
    a sniff_id without raising, even for parameters that exceed 32 MB."""
    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(0, 0, 1)])

    async def run() -> dict[str, Any]:
        r = await sniff.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000,
            max_duration_s=3600, sample_rate_hz=100_000_000.0,
            stream_decode=True,
        )
        await sniff.i2c_stop(r["sniff_id"])
        return r

    result = asyncio.run(run())
    assert "sniff_id" in result


# ---------------------------------------------------------------------------
# Step 3: Background callback exception propagates (1 test)
# ---------------------------------------------------------------------------


def test_sniff_streaming_background_decoder_error_surfaces_at_stop(
    tmp_path: Path,
) -> None:
    """A decoder.feed() exception raised in the background record_loop must
    surface as a RuntimeError at *_stop time matching 'sniff capture failed'."""
    from dwf_mcp.instruments.decoder.spi import SpiDecoder

    class _FailDecoder(SpiDecoder):
        def feed(self, chunk: np.ndarray) -> list[Any]:
            raise RuntimeError("synthetic decoder failure")

    dev = _reset_device(tmp_path)
    fake: FakeBackend = dev.backend  # type: ignore
    samples, _ = _spi_samples([0xA5])
    fake.set_logic_record_chunks([samples])
    fake.set_logic_record_status_sequence([(len(samples), 0, 0)])

    sniff_inst = Sniff(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run() -> None:
        r = await sniff_inst.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000,
            max_duration_s=0.5, stream_decode=True,
        )
        sniff_id = r["sniff_id"]
        # Monkey-patch the decoder on the already-started session.
        session = sniff_inst._spi_sessions[sniff_id]
        fail_dec = _FailDecoder()
        fail_dec.init({"clk": 0, "mosi": 1}, sample_rate_hz=1_000_000.0, mode=0)
        session.decoder = fail_dec
        # Overwrite the on_chunk_sync closure to use the failing decoder.
        session.record_session.on_chunk_sync = (
            lambda c, _s=session, _d=fail_dec: _s.transactions.extend(_d.feed(c))
        )
        # Wait for record_loop to consume the chunk and trigger the error.
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match="sniff capture failed.*synthetic decoder failure"):
            await sniff_inst.spi_stop(sniff_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Step 4: Final-drain callback exception propagates (1 test)
# ---------------------------------------------------------------------------


def test_sniff_streaming_final_drain_decoder_error_surfaces_at_stop(
    tmp_path: Path,
) -> None:
    """A decoder.feed() exception raised during _quiesce_and_drain's final
    drain (AFTER record_loop exits) must surface as RuntimeError at stop time
    matching 'sniff capture failed'.

    Setup: record_loop exits immediately (remaining=0); the drain call gets a
    poisoned chunk that the decoder rejects.
    """
    from dwf_mcp.instruments.decoder.spi import SpiDecoder

    class _FailDecoder(SpiDecoder):
        def feed(self, chunk: np.ndarray) -> list[Any]:
            raise RuntimeError("synthetic drain failure")

    dev = _reset_device(tmp_path)
    fake: FakeBackend = dev.backend  # type: ignore
    # record_loop exits immediately: available=0, remaining=0.
    fake.set_logic_record_status_sequence([(0, 0, 0)])
    # Drain sees available=5 on its one post-stop poll.
    poison_chunk, _ = _spi_samples([0xA5])
    fake._logic_record_canned_chunk = poison_chunk

    sniff_inst = Sniff(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run() -> None:
        r = await sniff_inst.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000,
            max_duration_s=0.5, stream_decode=True,
        )
        sniff_id = r["sniff_id"]
        session = sniff_inst._spi_sessions[sniff_id]
        fail_dec = _FailDecoder()
        fail_dec.init({"clk": 0, "mosi": 1}, sample_rate_hz=1_000_000.0, mode=0)
        session.decoder = fail_dec
        session.record_session.on_chunk_sync = (
            lambda c, _s=session, _d=fail_dec: _s.transactions.extend(_d.feed(c))
        )
        # Let record_loop poll once and finish (remaining=0).
        await asyncio.sleep(0.1)

        # Now make the drain call see a non-zero available count.
        # We inject an extra status entry that the drain's post-stop poll will see.
        fake._logic_record_status_sequence = [(len(poison_chunk), 0, 0)] + list(
            fake._logic_record_status_sequence
        )
        fake._logic_record_status_idx = 0

        with pytest.raises(RuntimeError, match="sniff capture failed.*synthetic drain failure"):
            await sniff_inst.spi_stop(sniff_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Step 5: Zero-transaction stream returns cleanly (1 test)
# ---------------------------------------------------------------------------


def test_sniff_streaming_zero_transactions_returns_empty_list(
    tmp_path: Path,
) -> None:
    """A streaming capture that produces no transactions must return an empty
    transaction list and NOT fall through to the chunks-iteration branch.

    Uses all-idle samples (all bits high → no I2C edges → decoder emits nothing).
    The branch must be controlled by session.streaming_decode, NOT by
    bool(session.transactions).
    """
    idle_chunk = np.ones((200, 16), dtype=np.uint8)  # all lines HIGH → no edges

    dev = _reset_device(tmp_path)
    fake: FakeBackend = dev.backend  # type: ignore
    fake.set_logic_record_chunks([idle_chunk])
    fake.set_logic_record_status_sequence([(len(idle_chunk), 0, 0)])

    sniff_inst = Sniff(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run() -> dict[str, Any]:
        r = await sniff_inst.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.5,
            sample_rate_hz=1_000_000.0, stream_decode=True,
        )
        await asyncio.sleep(0.1)
        return await sniff_inst.i2c_stop(r["sniff_id"])

    result = asyncio.run(run())
    assert result["count"] == 0
    assert result["lost_samples"] == 0


# ---------------------------------------------------------------------------
# Step 6: samples_received accuracy in stream mode (1 test)
# ---------------------------------------------------------------------------


def test_sniff_streaming_status_reports_samples_received(
    tmp_path: Path,
) -> None:
    """i2c_status must report samples_received > 0 after the record_loop has
    consumed at least one chunk, and the final stop must show total samples
    accumulated without accumulating in record_session.chunks."""
    chunk_size = 200
    idle_chunk = np.ones((chunk_size, 16), dtype=np.uint8)
    chunks = [idle_chunk.copy() for _ in range(3)]

    dev = _reset_device(tmp_path)
    fake: FakeBackend = dev.backend  # type: ignore
    # Give 3 chunks, each with remaining=1 except the last.
    status_seq = [(chunk_size, 0, 1), (chunk_size, 0, 1), (chunk_size, 0, 0)]
    fake.set_logic_record_status_sequence(status_seq)
    fake.set_logic_record_chunks(chunks)

    sniff_inst = Sniff(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        r = await sniff_inst.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.5,
            sample_rate_hz=1_000_000.0, stream_decode=True,
        )
        sniff_id = r["sniff_id"]
        await asyncio.sleep(0.1)  # let record_loop consume some chunks
        status = sniff_inst.i2c_status(sniff_id)
        stop_result = await sniff_inst.i2c_stop(sniff_id)
        return status, stop_result

    status, stop_result = asyncio.run(run())
    assert status["samples_received"] > 0, (
        "expected samples_received > 0 after record_loop ran"
    )


# ---------------------------------------------------------------------------
# Step 7: r.chunks empty throughout stream-mode capture (1 test)
# ---------------------------------------------------------------------------


def test_sniff_streaming_record_session_chunks_stay_empty(
    tmp_path: Path,
) -> None:
    """In stream mode, process_chunk routes through on_chunk_sync; chunks must
    NOT accumulate in record_session.chunks. Verify both mid-capture (via
    i2c_status) and post-stop (the session is gone, but we inspect during
    capture via the session reference we hold directly)."""
    chunk_size = 100
    idle_chunk = np.ones((chunk_size, 16), dtype=np.uint8)
    chunks = [idle_chunk.copy() for _ in range(3)]

    dev = _reset_device(tmp_path)
    fake: FakeBackend = dev.backend  # type: ignore
    status_seq = [(chunk_size, 0, 1), (chunk_size, 0, 1), (chunk_size, 0, 0)]
    fake.set_logic_record_status_sequence(status_seq)
    fake.set_logic_record_chunks(chunks)

    sniff_inst = Sniff(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run() -> tuple[list[np.ndarray], bool]:
        r = await sniff_inst.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=0.5,
            sample_rate_hz=1_000_000.0, stream_decode=True,
        )
        sniff_id = r["sniff_id"]
        session = sniff_inst._async_sessions[sniff_id]
        await asyncio.sleep(0.1)
        # Capture chunks list at mid-capture (should be empty in stream mode).
        mid_chunks = list(session.record_session.chunks)
        await sniff_inst.i2c_stop(sniff_id)
        pins_released = len(dev.allocator._observe_claims) == 0
        return mid_chunks, pins_released

    mid_chunks, pins_released = asyncio.run(run())
    assert mid_chunks == [], (
        f"expected record_session.chunks == [] during stream capture, got {len(mid_chunks)} items"
    )
    assert pins_released, "allocator observe claim should be released after i2c_stop"


# ---------------------------------------------------------------------------
# Step 8: Memory bound under streaming (1 test, tracemalloc)
# ---------------------------------------------------------------------------


def test_sniff_streaming_peak_memory_bounded(tmp_path: Path) -> None:
    """A streaming capture that discards all decoded output must not accumulate
    raw samples in memory. Peak usage must stay under 8 MB even when >32 MB of
    synthetic samples are fed through the pipeline.

    Uses a drop-all stub decoder so session.transactions stays empty, and the
    accumulation path would need >32 MB of chunks — proving streaming avoids it.
    """
    from dwf_mcp.instruments.decoder.i2c import I2cDecoder

    class _DropDecoder(I2cDecoder):
        def feed(self, chunk: np.ndarray) -> list[Any]:
            return []  # drop everything; never accumulates

        def finalize(self) -> list[Any]:
            return []

    # 4 chunks of 500_000 samples × 16 columns = 8 MB per chunk → 32 MB total
    # (just at the accumulation cap; streaming should stay well under).
    chunk_size = 500_000
    n_chunks = 4
    idle_chunk = np.ones((chunk_size, 16), dtype=np.uint8)
    chunks = [idle_chunk.copy() for _ in range(n_chunks)]

    dev = _reset_device(tmp_path)
    fake: FakeBackend = dev.backend  # type: ignore
    status_seq = [(chunk_size, 0, 1)] * (n_chunks - 1) + [(chunk_size, 0, 0)]
    fake.set_logic_record_status_sequence(status_seq)
    fake.set_logic_record_chunks(chunks)

    sniff_inst = Sniff(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))

    async def run() -> None:
        r = await sniff_inst.i2c_start(
            sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, max_duration_s=60.0,
            sample_rate_hz=1_000_000.0, stream_decode=True,
        )
        sniff_id = r["sniff_id"]
        # Swap in the drop-all decoder BEFORE record_loop has a chance to run.
        session = sniff_inst._async_sessions[sniff_id]
        drop_dec = _DropDecoder()
        drop_dec.init({"sda": 0, "scl": 1}, sample_rate_hz=1_000_000.0)
        session.decoder = drop_dec
        session.record_session.on_chunk_sync = (
            lambda c, _s=session, _d=drop_dec: _s.transactions.extend(_d.feed(c))
        )
        await asyncio.sleep(0.3)
        await sniff_inst.i2c_stop(sniff_id)

    tracemalloc.start()
    asyncio.run(run())
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 8 * 1024 * 1024, (
        f"peak memory {peak / 1e6:.1f} MB exceeds 8 MB bound — "
        "streaming path may be accumulating raw chunks"
    )


def test_tick_idle_reaps_completed_orphan_spi_session(sniff: Sniff) -> None:
    """A SPI sniff session that auto-completed but was never *_stop'd must be
    reaped (claim released, session evicted) by tick_idle — which the server
    now calls on every tool, so cleanup happens even if the client switches to
    non-sniff tools."""
    import time as _time

    from dwf_mcp.instruments._async_sniff import SNIFF_REAP_AFTER_S

    fake: FakeBackend = sniff.device.backend  # type: ignore
    fake.set_logic_record_status_sequence([(10, 0, 0)])  # completes after one poll

    async def run() -> str:
        start = await sniff.spi_start(
            clk_pin="dio0", mosi_pin="dio1", mode=0, freq_hz=100_000, max_duration_s=0.1,
        )
        await asyncio.sleep(0.05)  # let record_loop finish → record_session.done
        return start["sniff_id"]

    sniff_id = asyncio.run(run())
    session = sniff._spi_sessions[sniff_id]
    assert session.record_session.done
    # Backdate completion past the retention window so one reap pass evicts it.
    session.completed_at = _time.monotonic() - SNIFF_REAP_AFTER_S - 1.0

    claimed_before = dict(sniff.device.allocator.claimed_pins())
    sniff.tick_idle()

    assert sniff_id not in sniff._spi_sessions, "orphan session not reaped"
    # The observer claim it held must be released.
    assert sniff.device.allocator.claimed_pins() != claimed_before or claimed_before == {}
