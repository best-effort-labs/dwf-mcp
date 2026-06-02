from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


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
