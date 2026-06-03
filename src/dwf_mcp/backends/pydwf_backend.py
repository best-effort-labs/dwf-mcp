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

import numpy as np
from pydwf import (  # type: ignore[import-untyped]
    DwfAcquisitionMode,
    DwfAnalogCoupling,
    DwfLibrary,
    DwfState,
    DwfTriggerSlope,
    DwfTriggerSource,
)

from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfBackendError

log = logging.getLogger(__name__)


class PydwfBackend(DwfBackend):
    """Backend backed by pydwf / libdwf. Requires pydwf installed (a hard dep)."""

    def __init__(self) -> None:
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

    # --- Scope (AnalogIn) ---------------------------------------------------

    @property
    def _analog_in(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.analogIn

    def scope_configure(
        self, channel: int, range_v: float, offset_v: float, coupling: str, enable: bool
    ) -> None:
        ch_idx = channel - 1  # pydwf is 0-indexed
        ain = self._analog_in
        ain.channelEnableSet(ch_idx, enable)
        if enable:
            ain.channelRangeSet(ch_idx, range_v)
            ain.channelOffsetSet(ch_idx, offset_v)
            cp = DwfAnalogCoupling.DC if coupling == "DC" else DwfAnalogCoupling.AC
            ain.channelCouplingSet(ch_idx, cp)

    def scope_set_acquisition(self, sample_rate_hz: float, buffer_size: int, mode: str) -> None:
        ain = self._analog_in
        ain.frequencySet(sample_rate_hz)
        ain.bufferSizeSet(buffer_size)
        # Only "Single" supported in v1. Streaming is stage 3.
        if mode != "Single":
            raise ValueError(f"only Single mode supported in v1, got {mode!r}")
        ain.acquisitionModeSet(DwfAcquisitionMode.Single)

    def scope_set_trigger(
        self,
        source: str,
        channel: int | None,
        level_v: float,
        condition: str,
        position_s: float,
        timeout_s: float,
    ) -> None:
        ain = self._analog_in
        src_map = {
            "none": DwfTriggerSource.None_,
            "detector_analog_in": DwfTriggerSource.DetectorAnalogIn,
            "external1": DwfTriggerSource.External1,
            "external2": DwfTriggerSource.External2,
        }
        ain.triggerSourceSet(src_map[source])
        if channel is not None:
            ain.triggerChannelSet(channel - 1)
        ain.triggerLevelSet(level_v)
        slope = (
            DwfTriggerSlope.Rise
            if condition == "Rising"
            else (DwfTriggerSlope.Fall if condition == "Falling" else DwfTriggerSlope.Either)
        )
        ain.triggerConditionSet(slope)
        ain.triggerPositionSet(position_s)
        ain.triggerAutoTimeoutSet(timeout_s)

    def scope_arm(self) -> None:
        self._analog_in.configure(False, True)  # reconfigure=False, start=True

    def scope_status(self) -> str:
        st = self._analog_in.status(True)  # readData=True
        # Map DwfState enum to our string. We care about "Done"; map the rest as their name.
        if st == DwfState.Done:
            return "Done"
        return str(getattr(st, "name", st))

    def scope_read(self, channel: int, count: int) -> np.ndarray[Any, Any]:
        return np.asarray(self._analog_in.statusData(channel - 1, count), dtype=np.float64)
