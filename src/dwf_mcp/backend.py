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
