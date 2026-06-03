from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np


class DwfBackendError(Exception):
    """Raised for backend-level failures (enumeration, open, close)."""


class DwfDeviceLost(DwfBackendError):
    """Raised when the device disappears mid-session (unplug)."""


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    model: str
    firmware: str
    sample_rate_max_hz: float
    dio_count: int
    analog_in_channels: int
    analog_out_channels: int


class DwfBackend(ABC):
    @abstractmethod
    def enumerate(self) -> list[DeviceInfo]: ...

    @abstractmethod
    def open(self, serial: str | None = None) -> DeviceInfo: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    # Scope (AnalogIn) — added in stage 2.
    def scope_configure(
        self, channel: int, range_v: float, offset_v: float, coupling: str, enable: bool
    ) -> None:
        raise NotImplementedError

    def scope_set_acquisition(self, sample_rate_hz: float, buffer_size: int, mode: str) -> None:
        raise NotImplementedError

    def scope_set_trigger(self, source: str, channel: int | None, level_v: float,
                          condition: str, position_s: float, timeout_s: float) -> None:
        raise NotImplementedError

    def scope_arm(self) -> None:
        raise NotImplementedError

    def scope_status(self) -> str:
        raise NotImplementedError

    def scope_read(self, channel: int, count: int) -> np.ndarray[Any, Any]:
        raise NotImplementedError

    # Supply (AnalogIO) — added in stage 2.
    def supply_discover_nodes(self) -> dict[str, tuple[int, dict[str, int]]]:
        raise NotImplementedError

    def supply_node_set(self, channel: int, node: int, value: float) -> None:
        raise NotImplementedError

    def supply_node_get(self, channel: int, node: int) -> float:
        raise NotImplementedError

    def supply_master_enable(self, enabled: bool) -> None:
        raise NotImplementedError

    # I2C (ProtocolI2C) — added in stage 2.
    def i2c_configure(self, scl_pin_idx: int, sda_pin_idx: int, rate_hz: float,
                      stretch: bool, timeout_s: float) -> None:
        raise NotImplementedError

    def i2c_reset(self) -> None:
        raise NotImplementedError

    def i2c_write(self, address: int, data: bytes) -> int:
        raise NotImplementedError

    def i2c_read(self, address: int, length: int) -> bytes:
        raise NotImplementedError

    def i2c_write_read(self, address: int, write_data: bytes, read_length: int) -> bytes:
        raise NotImplementedError

    def i2c_write_one(self, address: int, byte: int) -> int:
        raise NotImplementedError

    # AWG (AnalogOut) — added in stage 3a.
    def awg_configure(
        self, channel: int, function: str, freq_hz: float,
        amplitude_v: float, offset_v: float, phase_deg: float,
        symmetry: float, run_time_s: float | None,
    ) -> None:
        raise NotImplementedError

    def awg_upload_custom(self, channel: int, samples: np.ndarray) -> None:
        raise NotImplementedError

    def awg_start(self, channel: int) -> None:
        raise NotImplementedError

    def awg_stop(self, channel: int) -> None:
        raise NotImplementedError

    # Pattern (DigitalOut) — added in stage 3a.
    def pattern_configure(
        self, pin_idx: int, function: str, freq_hz: float,
        duty: float, idle_state: str,
    ) -> None:
        raise NotImplementedError

    def pattern_start(self, pin_idx: int) -> None:
        raise NotImplementedError

    def pattern_stop(self, pin_idx: int) -> None:
        raise NotImplementedError

    # DIO (DigitalIO) — added in stage 3a.
    def dio_set_direction(self, pin_idx: int, output: bool) -> None:
        raise NotImplementedError

    def dio_set(self, pin_idx: int, state: bool) -> None:
        raise NotImplementedError

    def dio_read(self, pin_idx: int) -> bool:
        raise NotImplementedError

    # Logic buffer-mode (DigitalIn) — added in stage 3a.
    def logic_configure(
        self, pin_mask: int, sample_rate_hz: float, buffer_size: int
    ) -> None:
        raise NotImplementedError

    def logic_set_trigger(
        self, source: str, pin_idx: int | None, level: float | None,
        condition: str | None, position_s: float | None, timeout_s: float | None,
    ) -> None:
        raise NotImplementedError

    def logic_arm(self) -> None:
        raise NotImplementedError

    def logic_status(self) -> str:
        raise NotImplementedError

    def logic_read(self, count: int) -> np.ndarray:
        raise NotImplementedError

    # Logic record-mode (DigitalIn streaming) — added in stage 3a.
    def logic_record_configure(self, pin_mask: int, sample_rate_hz: float) -> None:
        raise NotImplementedError

    def logic_record_arm(self) -> None:
        raise NotImplementedError

    def logic_record_status(self) -> tuple[int, int, int]:
        raise NotImplementedError

    def logic_record_read(self, count: int) -> np.ndarray:
        raise NotImplementedError

    def logic_record_stop(self) -> None:
        raise NotImplementedError
