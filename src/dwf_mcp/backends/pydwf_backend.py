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
    DwfEnumFilter,
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

    # DwfEnumFilter.Type tells libdwf to interpret the low bits as connection-type flags
    # rather than the legacy device-type codes (where 1 = Electronics Explorer).
    # Combining with USB skips the local mDNS/UDP network scan that triggers the macOS
    # "dwf wants to use the network" permission prompt on first run.
    _ENUM_FILTER = DwfEnumFilter.Type | DwfEnumFilter.USB

    def enumerate(self) -> list[DeviceInfo]:
        enum = self._dwf.deviceEnum
        count = enum.enumerateDevices(self._ENUM_FILTER)
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
        count = enum.enumerateDevices(self._ENUM_FILTER)
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

    # --- Supply (AnalogIO) --------------------------------------------------

    @property
    def _analog_io(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.analogIO

    def supply_discover_nodes(self) -> dict[str, tuple[int, dict[str, int]]]:
        aio = self._analog_io
        aio.reset()
        layout: dict[str, tuple[int, dict[str, int]]] = {}
        ch_count = aio.channelCount()
        for ch_idx in range(ch_count):
            # channelName returns (name, label) per pydwf docs; [0] is the long name.
            ch_name = aio.channelName(ch_idx)[0].lower()
            # Map AD3 supply channel labels to our rail names.
            rail: str | None = None
            if "v+" in ch_name or "positive" in ch_name or "vpos" in ch_name:
                rail = "vpos"
            elif "v-" in ch_name or "negative" in ch_name or "vneg" in ch_name:
                rail = "vneg"
            if rail is None:
                continue
            node_count = aio.channelInfo(ch_idx)  # returns int directly
            nodes: dict[str, int] = {}
            for node_idx in range(node_count):
                # channelNodeName returns (name, units) per pydwf docs; [0] is the name.
                node_name = aio.channelNodeName(ch_idx, node_idx)[0].lower()
                if "enable" in node_name:
                    nodes["enable"] = node_idx
                elif "voltage" in node_name:
                    nodes["voltage"] = node_idx
                elif "current" in node_name:
                    nodes["current"] = node_idx
            if {"enable", "voltage"} <= set(nodes.keys()):
                layout[rail] = (ch_idx, nodes)
        if not layout:
            raise DwfBackendError("could not discover supply layout on AnalogIO")
        return layout

    def supply_node_set(self, channel: int, node: int, value: float) -> None:
        self._analog_io.channelNodeSet(channel, node, value)

    def supply_node_get(self, channel: int, node: int) -> float:
        aio = self._analog_io
        aio.status()  # refresh
        return float(aio.channelNodeStatus(channel, node))

    def supply_master_enable(self, enabled: bool) -> None:
        self._analog_io.enableSet(enabled)
        self._analog_io.configure()

    # --- I2C (ProtocolI2C) --------------------------------------------------

    @property
    def _i2c(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.i2c

    def i2c_configure(self, scl_pin_idx: int, sda_pin_idx: int, rate_hz: float,
                      stretch: bool, timeout_s: float) -> None:
        i2c = self._i2c
        i2c.sclSet(scl_pin_idx)
        i2c.sdaSet(sda_pin_idx)
        i2c.rateSet(rate_hz)
        i2c.stretchSet(1 if stretch else 0)
        i2c.timeoutSet(timeout_s)

    def i2c_reset(self) -> None:
        self._i2c.reset()

    def i2c_write(self, address: int, data: bytes) -> int:
        # pydwf FDwfDigitalI2cWrite expects an 8-bit address (7-bit addr << 1).
        return int(self._i2c.write(address << 1, list(data)))

    def i2c_read(self, address: int, length: int) -> bytes:
        # Returns Tuple[int, List[int]]: (nak, data).
        _nak, rx = self._i2c.read(address << 1, length)
        return bytes(rx)

    def i2c_write_read(self, address: int, write_data: bytes, read_length: int) -> bytes:
        # Returns Tuple[int, List[int]]: (nak, data).
        _nak, rx = self._i2c.writeRead(address << 1, list(write_data), read_length)
        return bytes(rx)

    def i2c_write_one(self, address: int, byte: int) -> int:
        return int(self._i2c.writeOne(address << 1, byte))

    # --- AWG (AnalogOut) ----------------------------------------------------

    @property
    def _analog_out(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.analogOut

    def awg_configure(
        self, channel: int, function: str, freq_hz: float,
        amplitude_v: float, offset_v: float, phase_deg: float,
        symmetry: float, run_time_s: float | None,
    ) -> None:
        from pydwf import DwfAnalogOutFunction, DwfAnalogOutNode  # type: ignore[import-untyped]
        ch_idx = channel - 1
        ao = self._analog_out
        node = DwfAnalogOutNode.Carrier
        func_map = {
            "Sine":     DwfAnalogOutFunction.Sine,
            "Square":   DwfAnalogOutFunction.Square,
            "Triangle": DwfAnalogOutFunction.Triangle,
            "RampUp":   DwfAnalogOutFunction.RampUp,
            "RampDown": DwfAnalogOutFunction.RampDown,
            "DC":       DwfAnalogOutFunction.DC,
            "Noise":    DwfAnalogOutFunction.Noise,
            "Custom":   DwfAnalogOutFunction.Custom,
        }
        ao.nodeEnableSet(ch_idx, node, True)
        ao.nodeFunctionSet(ch_idx, node, func_map[function])
        ao.nodeFrequencySet(ch_idx, node, freq_hz)
        ao.nodeAmplitudeSet(ch_idx, node, amplitude_v)
        ao.nodeOffsetSet(ch_idx, node, offset_v)
        ao.nodePhaseSet(ch_idx, node, phase_deg)
        ao.nodeSymmetrySet(ch_idx, node, symmetry)
        ao.runSet(ch_idx, run_time_s if run_time_s is not None else 0.0)
        # Apply params to hardware without starting output.
        ao.configure(ch_idx, False)

    def awg_upload_custom(self, channel: int, samples: np.ndarray) -> None:
        from pydwf import DwfAnalogOutNode  # type: ignore[import-untyped]
        ch_idx = channel - 1
        ao = self._analog_out
        node = DwfAnalogOutNode.Carrier
        ao.nodeEnableSet(ch_idx, node, True)
        ao.nodeDataSet(ch_idx, node, samples.tolist())
        ao.configure(ch_idx, False)

    def awg_start(self, channel: int) -> None:
        self._analog_out.configure(channel - 1, True)

    def awg_stop(self, channel: int) -> None:
        self._analog_out.configure(channel - 1, False)

    # --- Pattern (DigitalOut) -----------------------------------------------

    @property
    def _digital_out(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalOut

    def pattern_configure(
        self, pin_idx: int, function: str, freq_hz: float,
        duty: float, idle_state: str,
    ) -> None:
        from pydwf import (  # type: ignore[import-untyped]
            DwfDigitalOutIdle, DwfDigitalOutType,
        )
        dout = self._digital_out
        type_map = {
            "Pulse":  DwfDigitalOutType.Pulse,
            "Clock":  DwfDigitalOutType.Clock,
            "Random": DwfDigitalOutType.Random,
            "Custom": DwfDigitalOutType.Custom,
        }
        idle_map = {
            "low":  DwfDigitalOutIdle.Low,
            "high": DwfDigitalOutIdle.High,
            "hiz":  DwfDigitalOutIdle.Init,  # Init = Hi-Z on AD3
        }
        dout.enableSet(pin_idx, True)
        dout.typeSet(pin_idx, type_map[function])
        dout.frequencySet(pin_idx, freq_hz)
        dout.dutyCycleSet(pin_idx, duty)
        dout.idleSet(pin_idx, idle_map[idle_state])

    def pattern_start(self, pin_idx: int) -> None:
        self._digital_out.configure(True)

    def pattern_stop(self, pin_idx: int) -> None:
        # configure(False) stops the global DigitalOut engine — all other running pins halt too.
        self._digital_out.enableSet(pin_idx, False)
        self._digital_out.configure(False)

    # --- DIO (DigitalIO) ----------------------------------------------------

    @property
    def _digital_io(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalIO

    def dio_set_direction(self, pin_idx: int, output: bool) -> None:
        dio = self._digital_io
        current_mask = int(dio.outputEnableGet())
        if output:
            new_mask = current_mask | (1 << pin_idx)
        else:
            new_mask = current_mask & ~(1 << pin_idx)
        dio.outputEnableSet(new_mask)

    def dio_set(self, pin_idx: int, state: bool) -> None:
        dio = self._digital_io
        current_out = int(dio.outputGet())
        if state:
            new_out = current_out | (1 << pin_idx)
        else:
            new_out = current_out & ~(1 << pin_idx)
        dio.outputSet(new_out)

    def dio_read(self, pin_idx: int) -> bool:
        dio = self._digital_io
        dio.status()  # refresh input state
        input_mask = int(dio.inputStatus())
        return bool(input_mask & (1 << pin_idx))

    # --- Logic buffer-mode (DigitalIn) --------------------------------------

    @property
    def _digital_in(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalIn

    def logic_configure(
        self, pin_mask: int, sample_rate_hz: float, buffer_size: int
    ) -> None:
        from pydwf import DwfAcquisitionMode  # type: ignore[import-untyped]
        din = self._digital_in
        divider = max(1, round(100_000_000 / sample_rate_hz))
        din.dividerSet(divider)
        din.bufferSizeSet(buffer_size)
        din.acquisitionModeSet(DwfAcquisitionMode.Single)

    def logic_set_trigger(
        self, source: str, pin_idx: int | None, level: float | None,
        condition: str | None, position_s: float | None, timeout_s: float | None,
    ) -> None:
        from pydwf import DwfTriggerSource  # type: ignore[import-untyped]
        din = self._digital_in
        src_map = {
            "none":                 DwfTriggerSource.None_,
            "detector_digital_in":  DwfTriggerSource.DetectorDigitalIn,
            "external1":            DwfTriggerSource.External1,
            "external2":            DwfTriggerSource.External2,
        }
        din.triggerSourceSet(src_map[source])
        if position_s is not None:
            din.triggerPositionSet(position_s)
        if timeout_s is not None:
            din.triggerAutoTimeoutSet(timeout_s)

    def logic_arm(self) -> None:
        self._digital_in.configure(False, True)

    def logic_status(self) -> str:
        from pydwf import DwfState  # type: ignore[import-untyped]
        st = self._digital_in.status(True)
        if st == DwfState.Done:
            return "Done"
        return str(getattr(st, "name", st))

    def logic_read(self, count: int) -> np.ndarray:
        raw = self._digital_in.statusData2(count)
        arr = np.array(raw, dtype=np.uint16)
        result = np.zeros((len(arr), 16), dtype=np.uint8)
        for bit in range(16):
            result[:, bit] = (arr >> bit) & 1
        return result

    # --- Logic record-mode (DigitalIn streaming) ----------------------------

    def logic_record_configure(self, pin_mask: int, sample_rate_hz: float) -> None:
        from pydwf import DwfAcquisitionMode  # type: ignore[import-untyped]
        din = self._digital_in
        divider = max(1, round(100_000_000 / sample_rate_hz))
        din.dividerSet(divider)
        din.acquisitionModeSet(DwfAcquisitionMode.Record)

    def logic_record_arm(self) -> None:
        self._digital_in.configure(False, True)

    def logic_record_status(self) -> tuple[int, int, int]:
        din = self._digital_in
        din.status(True)
        return tuple(din.statusRecord())  # type: ignore[return-value]

    def logic_record_read(self, count: int) -> np.ndarray:
        raw = self._digital_in.statusData2(count)
        arr = np.array(raw, dtype=np.uint16)
        result = np.zeros((len(arr), 16), dtype=np.uint8)
        for bit in range(16):
            result[:, bit] = (arr >> bit) & 1
        return result

    def logic_record_stop(self) -> None:
        self._digital_in.configure(False, False)
