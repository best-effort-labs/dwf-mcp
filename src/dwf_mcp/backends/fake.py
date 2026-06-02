from __future__ import annotations

from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfBackendError

_FAKE_DEVICE = DeviceInfo(
    serial="FAKE-AD3-0001",
    model="Analog Discovery 3",
    firmware="fake-1.0",
    sample_rate_max_hz=125_000_000,
    dio_count=16,
    analog_in_channels=2,
    analog_out_channels=2,
)


class FakeBackend(DwfBackend):
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self._devices = devices or [_FAKE_DEVICE]
        self._open_info: DeviceInfo | None = None

    def enumerate(self) -> list[DeviceInfo]:
        return list(self._devices)

    def open(self, serial: str | None = None) -> DeviceInfo:
        if self._open_info is not None:
            return self._open_info
        candidates = [d for d in self._devices if serial is None or d.serial == serial]
        if not candidates:
            raise DwfBackendError(f"no device matches serial {serial!r}")
        self._open_info = candidates[0]
        return self._open_info

    def close(self) -> None:
        self._open_info = None

    @property
    def is_open(self) -> bool:
        return self._open_info is not None

    # Test helpers
    def simulate_unplug(self) -> None:
        self._open_info = None
