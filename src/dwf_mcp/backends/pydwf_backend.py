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
            DwfDigitalOutIdle,
            DwfDigitalOutType,
        )
        dout = self._digital_out
        type_map = {
            "Pulse":  DwfDigitalOutType.Pulse,
            "Clock":  DwfDigitalOutType.Pulse,  # pydwf has no Clock type; Pulse generates a periodic waveform
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
        dout.enableSet(pin_idx, True)
        dout.typeSet(pin_idx, type_map[function])
        dout.dividerSet(pin_idx, 1)
        dout.counterSet(pin_idx, low_count, high_count)
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
        raw = self._digital_in.statusData2(0, count)
        arr = np.array(raw, dtype=np.uint16)
        result = np.zeros((len(arr), 16), dtype=np.uint8)
        for bit in range(16):
            result[:, bit] = (arr >> bit) & 1
        return result

    # --- Logic record-mode (DigitalIn streaming) ----------------------------

    def logic_record_configure(self, pin_mask: int, sample_rate_hz: float, duration_s: float) -> None:
        import time

        from pydwf import DwfAcquisitionMode  # type: ignore[import-untyped]
        din = self._digital_in
        divider = max(1, round(100_000_000 / sample_rate_hz))
        din.dividerSet(divider)
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
        raw = self._digital_in.statusData2(0, count)
        arr = np.array(raw, dtype=np.uint16)
        result = np.zeros((len(arr), 16), dtype=np.uint8)
        for bit in range(16):
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
        try:
            self._analog_in.configure(False, False)
        except Exception:
            pass

    # --- SPI (ProtocolSPI) ----------------------------------------------------

    @property
    def _spi(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.spi

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
        if cs_idx is not None:
            polarity = 0 if cs_polarity == "active_low" else 1
            spi.selectSet(cs_idx, polarity)

    def spi_transfer(self, data: bytes, assert_cs: bool) -> bytes:
        rx = self._spi.writeRead(1, 8, list(data))  # 1=MOSI/MISO, 8 bits/word
        return bytes(rx)

    def spi_write(self, data: bytes, assert_cs: bool) -> None:
        dcs = 1 if assert_cs else 0
        self._spi.write(dcs, len(data) * 8, list(data))

    def spi_read(self, length: int, assert_cs: bool) -> bytes:
        dcs = 1 if assert_cs else 0
        rx = self._spi.read(dcs, length * 8)
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
