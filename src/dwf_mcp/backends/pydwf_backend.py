"""Real pydwf-backed backend.

pydwf API mapping notes (verified against pydwf 1.1.x):
  DwfLibrary().deviceEnum.enumerateDevices()  -> device count (int)
  DwfLibrary().deviceEnum.serialNumber(i)     -> serial string  (plan said deviceSerialNumber)
  DwfLibrary().deviceEnum.deviceName(i)       -> model name string
  DwfLibrary().deviceControl.open(i)          -> DwfDevice handle with .close()
  DwfLibrary().getVersion()                   -> libdwf version string (best-effort firmware)

pydwf does not expose a per-device firmware version pre-open, so we fall back to
the libdwf runtime version string as a best-effort "firmware" field.
"""

from __future__ import annotations

import logging
from typing import Any

from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfBackendError

log = logging.getLogger(__name__)


class PydwfBackend(DwfBackend):
    """Backend backed by pydwf / libdwf. Imported lazily so unit tests can avoid it."""

    def __init__(self) -> None:
        from pydwf import DwfLibrary  # type: ignore[import-not-found]

        self._dwf = DwfLibrary()
        self._device: Any | None = None
        self._info: DeviceInfo | None = None

    def enumerate(self) -> list[DeviceInfo]:
        enum = self._dwf.deviceEnum
        count = enum.enumerateDevices()
        out: list[DeviceInfo] = []
        for i in range(count):
            try:
                serial = enum.serialNumber(i)
                name = enum.deviceName(i)
            except Exception as exc:
                log.warning("failed to enumerate device %d: %s", i, exc)
                continue
            out.append(
                DeviceInfo(
                    serial=serial,
                    model=name,
                    firmware="",  # filled on open (best-effort)
                    sample_rate_max_hz=125_000_000,  # AD3 nominal; refine on open
                    dio_count=16,
                    analog_in_channels=2,
                    analog_out_channels=2,
                )
            )
        return out

    def open(self, serial: str | None = None) -> DeviceInfo:
        if self._info is not None:
            return self._info
        enum = self._dwf.deviceEnum
        count = enum.enumerateDevices()
        target_index: int | None = None
        for i in range(count):
            if serial is None or enum.serialNumber(i) == serial:
                target_index = i
                break
        if target_index is None:
            raise DwfBackendError(f"no Digilent device matches serial {serial!r}")
        device = self._dwf.deviceControl.open(target_index)
        try:
            firmware = self._dwf.getVersion()
        except Exception:
            firmware = ""
        info = DeviceInfo(
            serial=enum.serialNumber(target_index),
            model=enum.deviceName(target_index),
            firmware=firmware,
            sample_rate_max_hz=125_000_000,
            dio_count=16,
            analog_in_channels=2,
            analog_out_channels=2,
        )
        self._device = device
        self._info = info
        return info

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception as exc:
                log.warning("error closing device: %s", exc)
            self._device = None
        self._info = None

    @property
    def is_open(self) -> bool:
        return self._info is not None
