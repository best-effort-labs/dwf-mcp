from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.uart import UART
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
def uart(device: DwfDevice, tmp_path: Path) -> UART:
    device.open()
    return UART(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_both_pins_claims_both(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0", rx_pin="dio1")
    claimed = uart.device.allocator.claimed_pins()
    assert set(claimed.keys()) == {"dio0", "dio1"}


def test_configure_tx_only(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0")
    assert set(uart.device.allocator.claimed_pins().keys()) == {"dio0"}


def test_configure_rx_only(uart: UART) -> None:
    uart.configure(baud_rate=115200, rx_pin="dio1")
    assert set(uart.device.allocator.claimed_pins().keys()) == {"dio1"}


def test_configure_neither_raises(uart: UART) -> None:
    with pytest.raises(ValueError, match="tx_pin or rx_pin"):
        uart.configure(baud_rate=115200)


def test_configure_calls_backend(uart: UART) -> None:
    uart.configure(baud_rate=9600, tx_pin="dio0", rx_pin="dio1",
                   data_bits=7, parity="odd", stop_bits=2)
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    cfg = fake.uart_calls[0]
    assert cfg[0] == "configure"
    assert cfg[1]["baud_rate"] == 9600
    assert cfg[1]["parity"] == "odd"
    assert cfg[1]["data_bits"] == 7
    assert cfg[1]["stop_bits"] == 2
    assert cfg[1]["tx_idx"] == 0
    assert cfg[1]["rx_idx"] == 1


def test_configure_releases_on_exception(uart: UART) -> None:
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.uart_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        uart.configure(baud_rate=115200, tx_pin="dio0")
    assert uart.device.allocator.claimed_pins() == {}


def test_reconfigure_failed_leaves_unconfigured(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0", rx_pin="dio1")
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    def raise_on_configure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hw error")
    fake.uart_configure = raise_on_configure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        uart.configure(baud_rate=9600, tx_pin="dio0", rx_pin="dio1")
    assert not uart._configured
    assert uart.device.allocator.claimed_pins() == {}


def test_write_sends_data(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0")
    result = uart.write(data=[0x48, 0x65, 0x6C, 0x6C, 0x6F])
    assert result == {"bytes_written": 5}
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    writes = [c for c in fake.uart_calls if c[0] == "write"]
    assert writes[0][1]["data"] == b"Hello"


def test_write_without_tx_pin_raises(uart: UART) -> None:
    uart.configure(baud_rate=115200, rx_pin="dio1")
    with pytest.raises(InstrumentNotConfigured, match="tx_pin"):
        uart.write(data=[0x01])


def test_read_returns_data_and_parity_flag(uart: UART) -> None:
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    fake.set_uart_canned_rx(b"\xDE\xAD", parity_error=True)
    uart.configure(baud_rate=115200, rx_pin="dio1")
    result = uart.read(length=2)
    assert result["data"] == [0xDE, 0xAD]
    assert result["data_hex"] == "dead"
    assert result["parity_error"] is True


def test_read_partial_on_timeout(uart: UART) -> None:
    fake: FakeBackend = uart.device.backend  # type: ignore[assignment]
    fake.set_uart_canned_rx(b"\x01")  # only 1 byte even though 4 requested
    uart.configure(baud_rate=115200, rx_pin="dio1")
    result = uart.read(length=4)
    assert result["data"] == [0x01]  # partial result, not an error


def test_read_without_rx_pin_raises(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0")
    with pytest.raises(InstrumentNotConfigured, match="rx_pin"):
        uart.read(length=1)


def test_unconfigured_raises(uart: UART) -> None:
    with pytest.raises(InstrumentNotConfigured):
        uart.write(data=[0x01])


def test_release_clears_state(uart: UART) -> None:
    uart.configure(baud_rate=115200, tx_pin="dio0", rx_pin="dio1")
    uart.release()
    assert not uart._configured
    assert uart.device.allocator.claimed_pins() == {}
