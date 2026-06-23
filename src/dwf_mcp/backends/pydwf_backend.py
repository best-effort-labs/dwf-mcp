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

import ctypes
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import numpy as np
from pydwf import (
    DwfAcquisitionMode,
    DwfAnalogCoupling,
    DwfAnalogOutNode,
    DwfEnumConfigInfo,
    DwfEnumFilter,
    DwfLibrary,
    DwfState,
    DwfTriggerSlope,
    DwfTriggerSource,
)

from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfBackendError
from dwf_mcp.devices.configs import DeviceConfig, resolve_config_index

log = logging.getLogger(__name__)


def _safe_int_call(fn: Any) -> int:
    try:
        return int(fn())
    except Exception:
        return 0


class PydwfBackend(DwfBackend):
    """Backend backed by pydwf / libdwf. Requires pydwf installed (a hard dep)."""

    def __init__(self) -> None:
        self._dwf = DwfLibrary()
        self._device: Any | None = None
        self._info: DeviceInfo | None = None
        # SPI chip-select state, set by spi_configure (None = no CS pin).
        self._spi_cs_idx: int | None = None
        self._spi_cs_assert_level = 0
        self._spi_cs_idle_level = 1
        # Config table of the open device (populated at open, for status reporting).
        self._configs: list[DeviceConfig] = []
        # Logic sample bits (16 or 32), set by logic_configure/logic_record_configure.
        self._logic_sample_bits: int = 16
        # Achieved logic sample rate (Hz), set by logic_configure; used by the
        # trigger path to convert a position in seconds to a sample count.
        self._logic_sample_rate_hz: float = 0.0

    def _query_configs(self, device_index: int) -> list[DeviceConfig]:
        ee = self._dwf.deviceEnum
        nc = ee.enumerateConfigurations(device_index)
        return [
            DeviceConfig(
                index=c,
                digital_in_buffer=int(ee.configInfo(c, DwfEnumConfigInfo.DigitalInBufferSize)),
                analog_in_buffer=int(ee.configInfo(c, DwfEnumConfigInfo.AnalogInBufferSize)),
                analog_out_buffer=int(ee.configInfo(c, DwfEnumConfigInfo.AnalogOutBufferSize)),
                digital_out_buffer=int(ee.configInfo(c, DwfEnumConfigInfo.DigitalOutBufferSize)),
            )
            for c in range(nc)
        ]

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
                    devid=int(enum.deviceType(i)[0]),
                    sample_rate_max_hz=125_000_000,  # AD3 nominal; refine on open
                    dio_count=16,
                    analog_in_channels=2,
                    analog_out_channels=2,
                )
            )
        return out

    def open(self, serial: str | None = None, device_config: str | None = None) -> DeviceInfo:
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
        # Pick a hardware configuration. On the AD1/AD2 (shared IOs) configs trade
        # off buffer allocation; the SDK default has a small DigitalIn buffer. The
        # caller's strategy ("max_digital_in" etc.) resolves to a config index via
        # the device's config table. DWF_DEVICE_CONFIG is a raw-index override.
        self._configs = self._query_configs(target_index)
        env = os.environ.get("DWF_DEVICE_CONFIG")
        config_index = int(env) if env else resolve_config_index(self._configs, device_config)
        try:
            device = self._dwf.deviceControl.open(target_index, config_index)
        except Exception:
            # A failed open can leave a stale handle that cascades into later opens;
            # clear all handles so the next attempt starts clean.
            try:
                self._dwf.deviceControl.closeAll()
            except Exception as exc:
                log.warning("closeAll after failed open: %s", exc)
            raise
        try:
            firmware = self._dwf.getVersion()
        except Exception:
            firmware = ""
        ai = device.analogIn
        di = device.digitalIn
        devid, _hwrev = enum.deviceType(target_index)
        analog_in_channels = _safe_int_call(ai.channelCount)
        has_analog_in = analog_in_channels > 0
        if has_analog_in:
            sample_rate_max_hz = float(ai.frequencyInfo()[1])
            analog_in_buffer_max = int(ai.bufferSizeInfo()[1])
            analog_out_channels = _safe_int_call(device.analogOut.count)
            # Output buffer maximum (custom-waveform capacity). Defensive: a
            # device that can't report it leaves 0, which disables the size check.
            try:
                ao_buffer_max = int(device.analogOut.nodeDataInfo(0, DwfAnalogOutNode.Carrier)[1])
            except Exception:
                ao_buffer_max = 0
        else:
            sample_rate_max_hz = 0.0
            analog_in_buffer_max = 0
            analog_out_channels = 0
            ao_buffer_max = 0
        try:
            do_buffer_max = int(device.digitalOut.dataInfo(0))
        except Exception:
            do_buffer_max = 0
        digital_in_rate_max_hz = float(di.internalClockInfo())
        digital_in_channels = int(di.bitsInfo())
        (pull_supported, pull_bank_global, drive_supported, amp_min, amp_max,
         amp_steps, slew_steps) = self._probe_dio_caps(device)
        info = DeviceInfo(
            serial=enum.serialNumber(target_index),
            model=enum.deviceName(target_index),
            firmware=firmware,
            devid=int(devid),
            sample_rate_max_hz=sample_rate_max_hz,
            dio_count=digital_in_channels,
            analog_in_channels=analog_in_channels,
            analog_out_channels=analog_out_channels,
            analog_in_buffer_max=analog_in_buffer_max,
            digital_in_buffer_max=int(di.bufferSizeInfo()),
            digital_word_width=digital_in_channels,
            analog_out_buffer_max=ao_buffer_max,
            digital_out_buffer_max=do_buffer_max,
            has_analog_in=has_analog_in,
            digital_in_rate_max_hz=digital_in_rate_max_hz,
            digital_in_channels=digital_in_channels,
            dio_pull_supported=pull_supported,
            dio_pull_bank_global=pull_bank_global,
            dio_drive_supported=drive_supported,
            dio_drive_amp_min=amp_min, dio_drive_amp_max=amp_max,
            dio_drive_amp_steps=amp_steps, dio_drive_slew_steps=slew_steps,
        )
        self._device = device
        self._info = info
        return info

    def _probe_dio_caps(
        self, device: Any
    ) -> tuple[bool, bool, bool, float, float, int, int]:
        dio = device.digitalIO
        pull_bank_global = False
        try:
            pu, pd = dio.pullInfo()
            mask = pu | pd  # settable pull bits (union of up/down, in case asymmetric)
            pull_supported = mask != 0
            # one settable bit => a single bank-wide control (ADP2230); many bits =>
            # per-pin (Digital Discovery, pullInfo=0x1FFFF).
            pull_bank_global = pull_supported and bin(mask).count("1") <= 1
        except Exception:
            pull_supported = False
        amp_min = amp_max = 0.0
        amp_steps = slew_steps = 0
        drive_supported = False
        try:
            lib, hdwf = dio.lib, dio.hdwf
            a0, a1 = ctypes.c_double(), ctypes.c_double()
            s0, s1 = ctypes.c_int(), ctypes.c_int()
            rc = lib.FDwfDigitalIODriveInfo(hdwf, ctypes.c_int(0),
                ctypes.byref(a0), ctypes.byref(a1), ctypes.byref(s0), ctypes.byref(s1))
            if rc and a1.value > 0:
                drive_supported = True
                amp_min, amp_max = a0.value, a1.value
                amp_steps, slew_steps = s0.value, s1.value
        except Exception:
            pass
        return (pull_supported, pull_bank_global, drive_supported,
                amp_min, amp_max, amp_steps, slew_steps)

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
        # Reflects whether we hold an open handle — NOT live USB presence. There is
        # no cheap, side-effect-free probe that detects a *physical* unplug while a
        # handle is held: libdwf serves device-parameter reads from host memory, and
        # re-enumeration keeps listing a device we have open even after it's pulled
        # (both empirically verified against an AD3, 2026-06). A real unplug instead
        # surfaces as a DwfLibraryError on the next genuine I/O call; recovery is to
        # waveforms.close + waveforms.open. See docs/troubleshooting.md.
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

    def scope_sample_rate_get(self) -> float:
        return float(self._analog_in.frequencyGet())

    # Scope record-mode (AnalogIn streaming) — added in stage 3b.

    def scope_record_configure(
        self,
        channels: list[int],
        range_v: float,
        offset_v: float,
        coupling: str,
        sample_rate_hz: float,
        duration_s: float,
    ) -> None:
        from pydwf.core.auxiliary.enum_types import DwfAcquisitionMode
        coupling_map = {
            "DC": DwfAnalogCoupling.DC,
            "AC": DwfAnalogCoupling.AC,
        }
        ai = self._analog_in
        for ch in (0, 1):  # 0-indexed; channels list uses 1-indexed
            ai.channelEnableSet(ch, (ch + 1) in channels)
            ai.channelRangeSet(ch, range_v)
            ai.channelOffsetSet(ch, offset_v)
            ai.channelCouplingSet(ch, coupling_map[coupling])
        ai.frequencySet(sample_rate_hz)
        ai.acquisitionModeSet(DwfAcquisitionMode.Record)
        ai.recordLengthSet(duration_s)

    def scope_record_arm(self) -> None:
        self._analog_in.configure(False, True)

    def scope_record_status(self) -> tuple[int, int, int]:
        from pydwf.core.auxiliary.enum_types import DwfState
        state = self._analog_in.status(True)
        available, lost, _ = self._analog_in.statusRecord()
        # statusRecord()'s third value is corrupt-sample count (always 0), not remaining.
        # Return 0 when the device signals Done so record_loop exits correctly.
        return int(available), int(lost), 0 if state == DwfState.Done else 1

    def scope_record_read(self, count: int) -> np.ndarray:
        ai = self._analog_in
        ch1 = ai.statusData(0, count)
        ch2 = ai.statusData(1, count)
        return np.column_stack([ch1, ch2]).astype(np.float64)

    def scope_record_stop(self) -> None:
        self._analog_in.reset()

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

    def i2c_spy_start(self) -> None:
        self._i2c.spyStart()

    def i2c_spy_status(self, max_data_size: int) -> tuple[int, int, list[int], int]:
        start, stop, data, nak = self._i2c.spyStatus(max_data_size)
        return int(start), int(stop), [int(b) for b in data], int(nak)

    def i2c_spy_stop(self) -> None:
        self._i2c.reset()

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
        from pydwf import DwfAnalogOutFunction, DwfAnalogOutNode
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
        from pydwf import DwfAnalogOutNode
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

    def awg_frequency_get(self, channel: int) -> float:
        from pydwf import DwfAnalogOutNode
        return float(self._analog_out.nodeFrequencyGet(channel - 1, DwfAnalogOutNode.Carrier))

    # --- Pattern (DigitalOut) -----------------------------------------------

    @property
    def _digital_out(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalOut

    def pattern_configure(
        self, bit_idx: int, function: str, freq_hz: float,
        duty: float, idle_state: str,
    ) -> None:
        from pydwf import (
            DwfDigitalOutIdle,
            DwfDigitalOutType,
        )
        dout = self._digital_out
        type_map = {
            "Pulse":  DwfDigitalOutType.Pulse,
            # pydwf has no Clock type; Pulse generates a periodic waveform
            "Clock":  DwfDigitalOutType.Pulse,
            "Random": DwfDigitalOutType.Random,
            "Custom": DwfDigitalOutType.Custom,
        }
        idle_map = {
            "low":  DwfDigitalOutIdle.Low,
            "high": DwfDigitalOutIdle.High,
            "hiz":  DwfDigitalOutIdle.Init,  # Init = Hi-Z on AD3
        }
        clock = dout.internalClockInfo()
        period = max(1, round(clock / freq_hz))
        high_count = max(1, round(period * duty))
        low_count = max(1, period - high_count)
        dout.enableSet(bit_idx, True)
        dout.typeSet(bit_idx, type_map[function])
        dout.dividerSet(bit_idx, 1)
        dout.counterSet(bit_idx, low_count, high_count)
        dout.idleSet(bit_idx, idle_map[idle_state])

    def pattern_start(self, bit_idx: int) -> None:
        self._digital_out.configure(True)

    def pattern_stop(self, bit_idx: int) -> None:
        # configure(False) stops the global DigitalOut engine — all other running pins halt too.
        self._digital_out.enableSet(bit_idx, False)
        self._digital_out.configure(False)

    # --- DIO (DigitalIO) ----------------------------------------------------

    @property
    def _digital_io(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalIO

    def dio_set_direction(self, bit_idx: int, output: bool) -> None:
        dio = self._digital_io
        current_mask = int(dio.outputEnableGet())
        new_mask = current_mask | 1 << bit_idx if output else current_mask & ~(1 << bit_idx)
        dio.outputEnableSet(new_mask)

    def dio_set(self, bit_idx: int, state: bool) -> None:
        dio = self._digital_io
        current_out = int(dio.outputGet())
        new_out = current_out | 1 << bit_idx if state else current_out & ~(1 << bit_idx)
        dio.outputSet(new_out)

    def dio_read(self, bit_idx: int) -> bool:
        dio = self._digital_io
        dio.status()  # refresh input state
        input_mask = int(dio.inputStatus())
        return bool(input_mask & (1 << bit_idx))

    def dio_set_voltage(self, volts: float) -> None:
        if self._device is None:
            raise DwfBackendError("device not open")
        ch, node = self._find_analog_io_node("Digital Voltage", "Voltage")
        self._device.analogIO.channelNodeSet(ch, node, volts)

    # DINPP scalar: 0.0=down, 0.5=none, 1.0=up (confirmed in wired Task 0 spike)
    _DINPP_LEVEL: dict[str, float] = {"down": 0.0, "none": 0.5, "up": 1.0}

    def dio_pull_set(self, bit_idx: int, mode: str) -> None:
        dio = self._digital_io
        if self._info is not None and self._info.dio_pull_bank_global:
            # Bank-global pull (ADP2230): the device applies one pull to the whole
            # bank, and pullGet expands any set to 0xFFFF — so per-bit read-modify-write
            # accumulates and can't be cleared. Write the whole DIO mask explicitly.
            full = (1 << self._info.dio_count) - 1
            up = full if mode in ("up", "keeper") else 0
            down = full if mode in ("down", "keeper") else 0
            dio.pullSet(up, down)
            return
        up, down = dio.pullGet()           # per-pin RMW; preserve other bits (e.g. DD bit 16)
        m = 1 << bit_idx
        up &= ~m
        down &= ~m
        if mode == "up":
            up |= m
        elif mode == "down":
            down |= m
        elif mode == "keeper":
            # Keeper (bus-hold) = both pull-up and pull-down asserted (WaveForms
            # convention; verified on ADP2230: holds last driven level when released).
            up |= m
            down |= m
        dio.pullSet(up, down)

    def din_pull_set(self, mode: str) -> None:
        if self._device is None:
            raise DwfBackendError("device not open")
        ch, node = self._find_analog_io_node("Digital Voltage", "DINPP")
        self._device.analogIO.channelNodeSet(ch, node, self._DINPP_LEVEL[mode])

    def dio_drive_set(self, bank: int, amps: float, slew: int) -> None:
        dio = self._digital_io
        rc = dio.lib.FDwfDigitalIODriveSet(
            dio.hdwf, ctypes.c_int(bank), ctypes.c_double(amps), ctypes.c_int(slew))
        if not rc:
            raise DwfBackendError("FDwfDigitalIODriveSet failed")

    def _find_analog_io_node(self, channel_label: str, node_label: str) -> tuple[int, int]:
        if self._device is None:
            raise DwfBackendError("device not open")
        aio = self._device.analogIO
        for c in range(aio.channelCount()):
            if aio.channelName(c)[0] == channel_label:
                for n in range(aio.channelInfo(c)):
                    if aio.channelNodeName(c, n)[0] == node_label:
                        return c, n
        raise DwfBackendError(f"analogIO node {channel_label}/{node_label} not found")

    # --- Logic buffer-mode (DigitalIn) --------------------------------------

    @property
    def _digital_in(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.digitalIn

    def logic_configure(
        self, pin_mask: int, sample_rate_hz: float, buffer_size: int
    ) -> None:
        from pydwf import DwfAcquisitionMode
        din = self._digital_in
        clock = float(din.internalClockInfo())
        divider = max(1, round(clock / sample_rate_hz))
        din.dividerSet(divider)
        sample_bits = 32 if pin_mask >> 16 else 16
        din.sampleFormatSet(sample_bits)
        din.bufferSizeSet(buffer_size)
        din.acquisitionModeSet(DwfAcquisitionMode.Single)
        self._logic_sample_bits = sample_bits
        # Achieved (post-divider) rate; the trigger path needs it to convert a
        # position in seconds to a sample count.
        self._logic_sample_rate_hz = clock / divider

    def logic_set_trigger(
        self, source: str, pin_idx: int | None, level: float | None,
        condition: str | None, position_s: float | None, timeout_s: float | None,
    ) -> None:
        from pydwf import DwfTriggerSource

        from ..logic_trigger import digital_trigger_masks, position_samples
        din = self._digital_in
        src_map = {
            "none":                 DwfTriggerSource.None_,
            "detector_digital_in":  DwfTriggerSource.DetectorDigitalIn,
            "external1":            DwfTriggerSource.External1,
            "external2":            DwfTriggerSource.External2,
        }
        din.triggerSourceSet(src_map[source])
        # Configure the edge detector (which pin, which edge). Without this the
        # DetectorDigitalIn source would never actually fire on the requested pin.
        level_low, level_high, edge_rise, edge_fall = digital_trigger_masks(pin_idx, condition)
        din.triggerSet(level_low, level_high, edge_rise, edge_fall)
        if position_s is not None:
            # DigitalIn trigger position is in SAMPLES (unsigned int), not seconds.
            din.triggerPositionSet(position_samples(position_s, self._logic_sample_rate_hz))
        if timeout_s is not None:
            din.triggerAutoTimeoutSet(timeout_s)

    def logic_arm(self) -> None:
        self._digital_in.configure(False, True)

    def logic_status(self) -> str:
        from pydwf import DwfState
        st = self._digital_in.status(True)
        if st == DwfState.Done:
            return "Done"
        return str(getattr(st, "name", st))

    def logic_read(self, count: int) -> np.ndarray:
        bits = self._logic_sample_bits
        # statusData2(offset, count) auto-uses sampleFormatGet() when sample_format is omitted;
        # passing sample_format explicitly avoids an extra SDK round-trip.
        raw = self._digital_in.statusData2(0, count, sample_format=bits)
        if bits == 32:
            arr: np.ndarray = np.array(raw, dtype=np.uint32)
        else:
            arr = np.array(raw, dtype=np.uint16)
        result = np.zeros((len(arr), bits), dtype=np.uint8)
        for bit in range(bits):
            result[:, bit] = (arr >> bit) & 1
        return result

    # --- Logic record-mode (DigitalIn streaming) ----------------------------

    def logic_record_configure(
        self, pin_mask: int, sample_rate_hz: float, duration_s: float
    ) -> None:
        import time

        from pydwf import DwfAcquisitionMode
        din = self._digital_in
        clock = float(din.internalClockInfo())
        divider = max(1, round(clock / sample_rate_hz))
        din.dividerSet(divider)
        sample_bits = 32 if pin_mask >> 16 else 16
        din.sampleFormatSet(sample_bits)
        self._logic_sample_bits = sample_bits
        din.acquisitionModeSet(DwfAcquisitionMode.Record)
        # DigitalIn has no recordLengthSet; stop is controlled by deadline in logic_record_status.
        self._logic_record_deadline: float | None = time.monotonic() + duration_s

    def logic_record_arm(self) -> None:
        self._digital_in.configure(False, True)

    def logic_record_status(self) -> tuple[int, int, int]:
        import time

        from pydwf.core.auxiliary.enum_types import DwfState
        din = self._digital_in
        state = din.status(True)
        available, lost, _ = din.statusRecord()
        deadline = getattr(self, "_logic_record_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            din.configure(False, False)
            self._logic_record_deadline = None
            return int(available), int(lost), 0
        return int(available), int(lost), 0 if state == DwfState.Done else 1

    def logic_record_read(self, count: int) -> np.ndarray:
        bits = self._logic_sample_bits
        raw = self._digital_in.statusData2(0, count, sample_format=bits)
        if bits == 32:
            arr: np.ndarray = np.array(raw, dtype=np.uint32)
        else:
            arr = np.array(raw, dtype=np.uint16)
        result = np.zeros((len(arr), bits), dtype=np.uint8)
        for bit in range(bits):
            result[:, bit] = (arr >> bit) & 1
        return result

    def logic_record_stop(self) -> None:
        self._digital_in.configure(False, False)

    # --- DMM (AnalogIn measurement) -------------------------------------------

    def dmm_configure(
        self, channel: int, range_v: float, coupling: str, n_averages: int
    ) -> None:
        ain = self._analog_in
        ch_idx = channel - 1
        ain.channelEnableSet(0, False)
        ain.channelEnableSet(1, False)
        ain.channelEnableSet(ch_idx, True)
        ain.channelRangeSet(ch_idx, range_v)
        ain.channelOffsetSet(ch_idx, 0.0)
        cp = DwfAnalogCoupling.DC if coupling == "DC" else DwfAnalogCoupling.AC
        ain.channelCouplingSet(ch_idx, cp)
        ain.frequencySet(1000.0)
        ain.bufferSizeSet(n_averages)
        ain.acquisitionModeSet(DwfAcquisitionMode.Single)

    def dmm_arm(self) -> None:
        self._analog_in.configure(False, True)

    def dmm_status(self) -> str:
        st = self._analog_in.status(True)
        return "Done" if st == DwfState.Done else str(getattr(st, "name", st))

    def dmm_read(self, channel: int, count: int) -> np.ndarray:
        return np.asarray(
            self._analog_in.statusData(channel - 1, count), dtype=np.float64
        )

    def dmm_stop(self) -> None:
        with suppress(Exception):
            self._analog_in.configure(False, False)

    # --- SPI (ProtocolSPI) ----------------------------------------------------

    @property
    def _spi(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.spi

    # SPI wire constants for the ProtocolSPI API:
    #   transfer_type 1 = standard MOSI/MISO (vs 0=SISO, 2=dual, 4=quad)
    #   bits_per_word 8 = one byte per data-word; payloads are passed as word lists.
    _SPI_TRANSFER_TYPE = 1
    _SPI_BITS_PER_WORD = 8

    def spi_configure(
        self, clk_idx: int, freq_hz: float, mode: int,
        mosi_idx: int | None, miso_idx: int | None, cs_idx: int | None,
        cs_polarity: str, bit_order: str,
    ) -> None:
        spi = self._spi
        spi.reset()
        spi.frequencySet(freq_hz)
        spi.modeSet(mode)
        spi.orderSet(1 if bit_order == "msb" else 0)
        spi.clockSet(clk_idx)
        if mosi_idx is not None:
            spi.dataSet(0, mosi_idx)   # DQ0 = MOSI
        if miso_idx is not None:
            spi.dataSet(1, miso_idx)   # DQ1 = MISO
        # CS is driven explicitly via select() around each transfer (see _cs_bracket),
        # so record the pin + asserted/idle levels rather than relying on auto-CS.
        self._spi_cs_idx = cs_idx
        if cs_idx is not None:
            active_low = cs_polarity == "active_low"
            self._spi_cs_assert_level = 0 if active_low else 1
            self._spi_cs_idle_level = 1 if active_low else 0
            spi.select(cs_idx, self._spi_cs_idle_level)  # park CS deasserted

    @contextmanager
    def _cs_bracket(self, assert_cs: bool) -> Iterator[None]:
        """Assert CS (active level) on entry and return it to idle on exit — even
        if the wrapped transfer raises — when assert_cs is set and a CS pin is
        configured. A no-op otherwise."""
        active = assert_cs and self._spi_cs_idx is not None
        if active:
            self._spi.select(self._spi_cs_idx, self._spi_cs_assert_level)
        try:
            yield
        finally:
            if active:
                self._spi.select(self._spi_cs_idx, self._spi_cs_idle_level)

    def spi_transfer(self, data: bytes, assert_cs: bool) -> bytes:
        with self._cs_bracket(assert_cs):
            rx = self._spi.writeRead(
                self._SPI_TRANSFER_TYPE, self._SPI_BITS_PER_WORD, list(data)
            )
        return bytes(rx)

    def spi_write(self, data: bytes, assert_cs: bool) -> None:
        with self._cs_bracket(assert_cs):
            self._spi.write(
                self._SPI_TRANSFER_TYPE, self._SPI_BITS_PER_WORD, list(data)
            )

    def spi_read(self, length: int, assert_cs: bool) -> bytes:
        with self._cs_bracket(assert_cs):
            rx = self._spi.read(
                self._SPI_TRANSFER_TYPE, self._SPI_BITS_PER_WORD, length
            )
        return bytes(rx)

    # --- UART (ProtocolUART) --------------------------------------------------

    @property
    def _uart(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.uart

    def uart_configure(
        self, baud_rate: int, tx_idx: int | None, rx_idx: int | None,
        data_bits: int, parity: str, stop_bits: int,
    ) -> None:
        uart = self._uart
        uart.reset()
        uart.rateSet(baud_rate)
        uart.bitsSet(data_bits)
        parity_map = {"none": 0, "odd": 1, "even": 2}
        uart.paritySet(parity_map[parity])
        uart.stopSet(stop_bits)
        if tx_idx is not None:
            uart.txSet(tx_idx)
            uart.tx(b"")   # force TX pin to UART idle (HIGH) before enabling RX
        if rx_idx is not None:
            uart.rxSet(rx_idx)
        uart.rx(0)   # initialize receiver
        uart.rx(1)   # activate DMA buffer; first non-zero call is always a loss
        import time as _time
        _time.sleep(0.010)   # WaveForms firmware needs ~10ms after rx(1) to start buffering

    def uart_write(self, data: bytes) -> None:
        self._uart.tx(data)

    def uart_read(self, length: int, timeout_s: float) -> tuple[bytes, bool]:
        import time
        deadline = time.monotonic() + timeout_s
        buf = b""
        parity_err = False
        while len(buf) < length and time.monotonic() < deadline:
            rx_data, pe = self._uart.rx(length - len(buf))
            if rx_data:
                buf += bytes(rx_data)
                parity_err = parity_err or bool(pe)
            else:
                time.sleep(0.002)
        return buf, parity_err

    def uart_sniff(
        self,
        rx_pin_idx: int,
        baud: int,
        data_bits: int,
        parity: str,
        stop_bits: int,
        duration_s: float,
        poll_interval_s: float,
        polarity: int,
    ) -> list[tuple[float, bytes, bool]]:
        import time
        uart = self._uart
        uart.reset()
        uart.rateSet(baud)
        uart.bitsSet(data_bits)
        parity_map = {"none": 0, "odd": 1, "even": 2}
        uart.paritySet(parity_map[parity])
        uart.stopSet(stop_bits)
        uart.polaritySet(polarity)
        uart.rxSet(rx_pin_idx)
        uart.rx(0)
        uart.rx(1)
        time.sleep(0.010)   # firmware needs ~10ms to start buffering after rx(1)
        try:
            frames: list[tuple[float, bytes, bool]] = []
            start_t = time.monotonic()
            deadline = start_t + duration_s
            while time.monotonic() < deadline:
                rx_data, pe = uart.rx(256)
                if rx_data:
                    ts = time.monotonic() - start_t
                    frames.append((ts, bytes(rx_data), bool(pe)))
                else:
                    time.sleep(poll_interval_s)
            return frames
        finally:
            uart.reset()

    # --- CAN (ProtocolCAN) ----------------------------------------------------

    @property
    def _can(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.can

    def can_configure(self, tx_idx: int, rx_idx: int, bit_rate: int) -> None:
        import time as _time
        can = self._can
        can.reset()
        can.rateSet(bit_rate)
        can.txSet(tx_idx)
        _time.sleep(0.010)   # let TX settle to CAN idle before enabling RX
        can.rxSet(rx_idx)
        can.rx()             # prime the receive buffer
        _time.sleep(0.010)

    def can_send(self, id: int, data: bytes, extended: bool) -> None:
        self._can.tx(id, bool(extended), False, data)

    def can_receive(self, timeout_s: float) -> tuple[int | None, bytes, bool, int]:
        import time
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame_id, ext, remote, data, status = self._can.rx()
            if status:
                return frame_id, bytes(data), bool(ext), status
            time.sleep(0.001)
        return None, b"", False, 0

    def can_sniff(
        self,
        rx_pin_idx: int,
        bitrate: int,
        duration_s: float,
        poll_interval_s: float,
    ) -> list[tuple[float, int, bytes, bool, int]]:
        import time
        can = self._can
        can.reset()
        can.rateSet(bitrate)
        can.rxSet(rx_pin_idx)
        can.rx()             # prime buffer
        time.sleep(0.010)    # let TX line settle to CAN idle before loop begins
        try:
            frames: list[tuple[float, int, bytes, bool, int]] = []
            start_t = time.monotonic()
            deadline = start_t + duration_s
            while time.monotonic() < deadline:
                frame_id, ext, _remote, data, status = can.rx()
                if status:
                    ts = time.monotonic() - start_t
                    frames.append((ts, int(frame_id), bytes(data), bool(ext), int(status)))
                else:
                    time.sleep(poll_interval_s)
            return frames
        finally:
            can.reset()
