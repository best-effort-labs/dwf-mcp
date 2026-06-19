from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfBackendError


@dataclass
class _BodeSim:
    ref_channel: int = 1
    dut_channel: int = 2
    fc_hz: float = 1000.0           # one-pole RC corner
    range_v: float = 5.0
    freq_quantize: float = 1.0      # actual_freq = requested * this (noncoherence test)
    rate_quantize: float = 1.0
    dut_delay_samples: float = 0.0  # extra sub-sample delay on the DUT channel
    dc_offset: float = 0.0
    noise_std: float = 0.0
    harmonic2: float = 0.0          # 2nd-harmonic fraction on the DUT channel
    clip: bool = False
    transient_first: bool = False   # corrupt the FIRST acquisition (warm-up sanity)
    # Runtime counter (scope_arm bumps it); excluded from __init__/repr/eq so it
    # can't be injected via set_bode_sim(**kwargs) and doesn't affect equality.
    _armed_count: int = field(default=0, init=False, repr=False, compare=False)

_FAKE_DEVICE = DeviceInfo(
    serial="FAKE-AD3-0001",
    model="Analog Discovery 3",
    firmware="fake-1.0",
    sample_rate_max_hz=100_000_000.0,
    dio_count=16,
    analog_in_channels=2,
    analog_out_channels=4,
    devid=10,
    analog_in_buffer_max=16384,
    digital_in_buffer_max=16384,
    digital_word_width=16,
    analog_out_buffer_max=16384,
    digital_out_buffer_max=2048,
)


def make_fake_device(
    devid: int = 10, *, serial: str = "FAKE-0001", model: str = "Fake Discovery",
    dio_count: int = 16, analog_in_channels: int = 2, analog_out_channels: int = 4,
    sample_rate_max_hz: float = 100_000_000.0, analog_in_buffer_max: int = 16384,
    digital_in_buffer_max: int = 16384, digital_word_width: int = 16,
    analog_out_buffer_max: int = 16384, digital_out_buffer_max: int = 2048,
) -> DeviceInfo:
    return DeviceInfo(
        serial=serial, model=model, firmware="fake-1.0", devid=devid,
        sample_rate_max_hz=sample_rate_max_hz, dio_count=dio_count,
        analog_in_channels=analog_in_channels, analog_out_channels=analog_out_channels,
        analog_in_buffer_max=analog_in_buffer_max,
        digital_in_buffer_max=digital_in_buffer_max, digital_word_width=digital_word_width,
        analog_out_buffer_max=analog_out_buffer_max,
        digital_out_buffer_max=digital_out_buffer_max,
    )


def make_dd_device(serial: str = "DD-0001") -> DeviceInfo:
    return DeviceInfo(
        serial=serial, model="Digital Discovery", firmware="fake-1.0", devid=4,
        sample_rate_max_hz=0.0, dio_count=24, analog_in_channels=0,
        analog_out_channels=0, has_analog_in=False,
        digital_in_rate_max_hz=800_000_000.0, digital_in_channels=24,
        digital_out_buffer_max=16384,
        dio_pull_supported=True, dio_drive_supported=True,
        dio_drive_amp_min=0.002, dio_drive_amp_max=0.016,
        dio_drive_amp_steps=6, dio_drive_slew_steps=3,
    )


class FakeBackend(DwfBackend):
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self._devices = devices or [_FAKE_DEVICE]
        self._open_info: DeviceInfo | None = None
        self.last_device_config: str | None = None
        # Scope (AnalogIn) state
        self.scope_calls: list[tuple[str, dict[str, Any]]] = []
        self._scope_canned: dict[int, np.ndarray[Any, Any]] = {}
        self._scope_status_sequence: list[str] = ["Done"]
        self._scope_status_idx = 0
        # Supply (AnalogIO) state
        self.supply_calls: list[tuple[str, dict[str, Any]]] = []
        self._supply_layout: dict[str, tuple[int, dict[str, int]]] = {
            "vpos": (0, {"enable": 0, "voltage": 1, "current": 2}),
            "vneg": (1, {"enable": 0, "voltage": 1, "current": 2}),
        }
        self._supply_setpoints: dict[tuple[int, int], float] = {}
        self._supply_canned_status: dict[tuple[int, int], float] = {}
        # I2C (ProtocolI2C) state
        self.i2c_calls: list[tuple[str, dict[str, Any]]] = []
        self._i2c_acks: dict[int, bool] = {}
        self._i2c_reads: dict[int, bytes] = {}
        # AWG (AnalogOut) state
        self.awg_calls: list[tuple[str, dict[str, Any]]] = []
        self._awg_freq: dict[int, float] = {}     # last configured freq per channel
        self._awg_amp: float = 0.0                # last configured amplitude
        self._scope_sample_rate: float = 0.0      # last set_acquisition rate
        self._scope_buffer: int = 0               # last set_acquisition buffer
        self._bode_sim: _BodeSim | None = None    # set by set_bode_sim (Task 4)
        # Single seeded RNG drawn across reads, so successive channel reads get
        # INDEPENDENT (but deterministic) noise — a fresh default_rng(0) per read
        # would hand ref and dut identical noise that cancels in the gain/phase ratio.
        self._bode_rng = np.random.default_rng(0)
        # Pattern (DigitalOut) state
        self.pattern_calls: list[tuple[str, dict[str, Any]]] = []
        # DIO (DigitalIO) state
        self.dio_calls: list[tuple[str, dict[str, Any]]] = []
        self._dio_pin_values: dict[int, bool] = {}
        self._dio_voltage: float = 3.3
        self.pull_up_mask: int = 0
        self.pull_down_mask: int = 0
        self.din_pull: str = "none"
        self.drive: tuple[int, float, int] | None = None
        # Logic buffer-mode state
        self.logic_calls: list[tuple[str, dict[str, Any]]] = []
        self._logic_status_sequence: list[str] = ["Done"]
        self._logic_status_idx = 0
        self._logic_canned_data: np.ndarray = np.zeros((0, 16), dtype=np.uint8)
        self.logic_pin_mask: int = 0
        self.logic_sample_bits: int = 16
        # Logic record-mode state
        self._logic_record_status_sequence: list[tuple[int, int, int]] = [(10, 0, 0)]
        self._logic_record_status_idx = 0
        self._logic_record_canned_chunk: np.ndarray = np.zeros((10, 16), dtype=np.uint8)
        self._logic_record_chunks_queue: list[np.ndarray] = []
        # Scope record-mode state
        self.scope_record_calls: list[tuple[str, dict[str, Any]]] = []
        self._scope_record_status_sequence: list[tuple[int, int, int]] = [(10, 0, 0)]
        self._scope_record_status_idx = 0
        self._scope_record_canned_chunk: np.ndarray = np.zeros((10, 2), dtype=np.float64)
        # DMM (AnalogIn measurement) state
        self.dmm_calls: list[tuple[str, dict[str, Any]]] = []
        self._dmm_status_sequence: list[str] = ["Done"]
        self._dmm_status_idx = 0
        self._dmm_canned_data: dict[int, np.ndarray] = {}
        # SPI (ProtocolSPI) state
        self.spi_calls: list[tuple[str, dict[str, Any]]] = []
        self._spi_canned_rx: bytes = b""
        # UART (ProtocolUART) state
        self.uart_calls: list[tuple[str, dict[str, Any]]] = []
        self._uart_canned_rx: bytes = b""
        self._uart_parity_error: bool = False
        # CAN (ProtocolCAN) state
        self.can_calls: list[tuple[str, dict[str, Any]]] = []
        self._can_canned_frame: tuple[int | None, bytes, bool, int] = (None, b"", False, 0)
        # Sniff state
        self._i2c_spy_sequence: list[tuple[int, int, list[int], int]] = []
        self._i2c_spy_idx: int = 0
        self._uart_sniff_frames: list[tuple[float, bytes, bool]] = []
        self._can_sniff_frames: list[tuple[float, int, bytes, bool, int]] = []
        self.sniff_calls: list[tuple[str, dict[str, Any]]] = []

    def enumerate(self) -> list[DeviceInfo]:
        return list(self._devices)

    def open(self, serial: str | None = None, device_config: str | None = None) -> DeviceInfo:
        # The fake has no hardware config table; record the requested strategy so
        # tests can assert it was plumbed through, but otherwise ignore it.
        self.last_device_config = device_config
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

    # --- Scope (AnalogIn) ---

    def scope_configure(
        self, channel: int, range_v: float, offset_v: float, coupling: str, enable: bool
    ) -> None:
        self.scope_calls.append(("configure", {
            "channel": channel, "range_v": range_v, "offset_v": offset_v,
            "coupling": coupling, "enable": enable,
        }))

    def scope_set_acquisition(self, sample_rate_hz: float, buffer_size: int, mode: str) -> None:
        self.scope_calls.append(("set_acquisition", {
            "sample_rate_hz": sample_rate_hz, "buffer_size": buffer_size, "mode": mode,
        }))
        self._scope_sample_rate = sample_rate_hz
        self._scope_buffer = buffer_size

    def scope_set_trigger(self, source: str, channel: int | None, level_v: float,
                          condition: str, position_s: float, timeout_s: float) -> None:
        self.scope_calls.append(("set_trigger", {
            "source": source, "channel": channel, "level_v": level_v,
            "condition": condition, "position_s": position_s, "timeout_s": timeout_s,
        }))

    def scope_arm(self) -> None:
        self.scope_calls.append(("arm", {}))
        self._scope_status_idx = 0
        if self._bode_sim is not None:
            self._bode_sim._armed_count += 1

    def scope_status(self) -> str:
        idx = min(self._scope_status_idx, len(self._scope_status_sequence) - 1)
        result = self._scope_status_sequence[idx]
        self._scope_status_idx += 1
        return result

    def scope_read(self, channel: int, count: int) -> np.ndarray[Any, Any]:
        if self._bode_sim is not None:
            return self._bode_sim_read(channel, count)
        if channel in self._scope_canned:
            return self._scope_canned[channel][:count]
        return np.zeros(count, dtype=np.float64)

    def _bode_sim_read(self, channel: int, count: int) -> np.ndarray[Any, Any]:
        sim = self._bode_sim
        assert sim is not None
        # Actual (quantized) values — exactly what the getters report.
        f = self._awg_freq.get(self._awg_active_channel(), 0.0) * sim.freq_quantize
        sr = self._scope_sample_rate * sim.rate_quantize
        amp = self._awg_amp
        nn = np.arange(count)
        t = nn / sr if sr else nn * 0.0
        ref = amp * np.cos(2 * np.pi * f * t)
        if channel == sim.ref_channel:
            sig = ref
        elif channel == sim.dut_channel:
            # one-pole RC: gain 1/sqrt(1+(f/fc)^2), phase -atan(f/fc)
            ratio = f / sim.fc_hz
            g = 1.0 / np.sqrt(1.0 + ratio * ratio)
            ph = -np.arctan(ratio)
            delay = 2 * np.pi * f * sim.dut_delay_samples / sr if sr else 0.0
            sig = amp * g * np.cos(2 * np.pi * f * t + ph - delay)
            if sim.harmonic2:
                # Post-RC distortion model: 2nd harmonic scaled by the fundamental's
                # gain g=H(f) (NOT H(2f)) — i.e. distortion injected after the filter.
                sig = sig + amp * g * sim.harmonic2 * np.cos(2 * 2 * np.pi * f * t)
        else:
            sig = np.zeros(count)
        sig = sig + sim.dc_offset
        if sim.noise_std:
            sig = sig + self._bode_rng.normal(0.0, sim.noise_std, count)
        if sim.transient_first and sim._armed_count <= 1:
            sig = sig + 5.0  # gross corruption on the first armed capture
        if sim.clip:
            sig = np.clip(sig, -sim.range_v, sim.range_v)
        return sig.astype(np.float64)

    def _awg_active_channel(self) -> int:
        return next(iter(self._awg_freq), 1)

    def scope_sample_rate_get(self) -> float:
        sr = self._scope_sample_rate
        return sr * self._bode_sim.rate_quantize if self._bode_sim else sr

    # Test helpers
    def set_scope_canned_data(self, channels: dict[int, np.ndarray[Any, Any]]) -> None:
        self._scope_canned = dict(channels)

    def set_scope_status_sequence(self, sequence: list[str]) -> None:
        self._scope_status_sequence = list(sequence)
        self._scope_status_idx = 0

    def simulate_unplug(self) -> None:
        self._open_info = None

    def set_bode_sim(self, **kwargs: Any) -> None:
        self._bode_sim = _BodeSim(**kwargs)

    # --- Supply (AnalogIO) ---

    def supply_discover_nodes(self) -> dict[str, tuple[int, dict[str, int]]]:
        return {k: (ch, dict(nodes)) for k, (ch, nodes) in self._supply_layout.items()}

    def supply_node_set(self, channel: int, node: int, value: float) -> None:
        self._supply_setpoints[(channel, node)] = value
        self.supply_calls.append(("node_set", {"channel": channel, "node": node, "value": value}))

    def supply_node_get(self, channel: int, node: int) -> float:
        if (channel, node) in self._supply_canned_status:
            return self._supply_canned_status[(channel, node)]
        return self._supply_setpoints.get((channel, node), 0.0)

    def supply_master_enable(self, enabled: bool) -> None:
        self.supply_calls.append(("master_enable", {"enabled": enabled}))

    # Test helpers
    def set_supply_canned_status(self, values: dict[tuple[int, int], float]) -> None:
        self._supply_canned_status = dict(values)

    # --- I2C (ProtocolI2C) ---

    def i2c_configure(self, scl_pin_idx: int, sda_pin_idx: int, rate_hz: float,
                      stretch: bool, timeout_s: float) -> None:
        self.i2c_calls.append(("configure", {
            "scl_pin_idx": scl_pin_idx, "sda_pin_idx": sda_pin_idx,
            "rate_hz": rate_hz, "stretch": stretch, "timeout_s": timeout_s,
        }))

    def i2c_reset(self) -> None:
        self.i2c_calls.append(("reset", {}))

    def i2c_write(self, address: int, data: bytes) -> int:
        self.i2c_calls.append(("write", {"address": address, "data": data}))
        return 0 if self._i2c_acks.get(address, False) else 1

    def i2c_read(self, address: int, length: int) -> bytes:
        self.i2c_calls.append(("read", {"address": address, "length": length}))
        canned = self._i2c_reads.get(address, b"")
        return canned[:length]

    def i2c_write_read(self, address: int, write_data: bytes, read_length: int) -> bytes:
        self.i2c_calls.append(("write_read", {
            "address": address, "write_data": write_data, "read_length": read_length,
        }))
        return self._i2c_reads.get(address, b"")[:read_length]

    def i2c_write_one(self, address: int, byte: int) -> int:
        self.i2c_calls.append(("write_one", {"address": address, "byte": byte}))
        return 0 if self._i2c_acks.get(address, False) else 1

    # Test helpers
    def set_i2c_acks(self, acks: dict[int, bool]) -> None:
        self._i2c_acks = dict(acks)

    def set_i2c_reads(self, reads: dict[int, bytes]) -> None:
        self._i2c_reads = dict(reads)

    # --- AWG (AnalogOut) ---

    def awg_configure(
        self, channel: int, function: str, freq_hz: float,
        amplitude_v: float, offset_v: float, phase_deg: float,
        symmetry: float, run_time_s: float | None,
    ) -> None:
        self.awg_calls.append(("configure", {
            "channel": channel, "function": function, "freq_hz": freq_hz,
            "amplitude_v": amplitude_v, "offset_v": offset_v,
            "phase_deg": phase_deg, "symmetry": symmetry, "run_time_s": run_time_s,
        }))
        self._awg_freq[channel] = freq_hz
        self._awg_amp = amplitude_v

    def awg_upload_custom(self, channel: int, samples: np.ndarray) -> None:
        self.awg_calls.append(("upload_custom", {"channel": channel, "n_samples": len(samples)}))

    def awg_start(self, channel: int) -> None:
        self.awg_calls.append(("start", {"channel": channel}))

    def awg_stop(self, channel: int) -> None:
        self.awg_calls.append(("stop", {"channel": channel}))

    def awg_frequency_get(self, channel: int) -> float:
        # Apply an optional quantization nudge so tests can exercise the noncoherent path.
        f = self._awg_freq.get(channel, 0.0)
        return f * self._bode_sim.freq_quantize if self._bode_sim else f

    # --- Pattern (DigitalOut) ---

    def pattern_configure(
        self, bit_idx: int, function: str, freq_hz: float,
        duty: float, idle_state: str,
    ) -> None:
        self.pattern_calls.append(("configure", {
            "bit_idx": bit_idx, "function": function, "freq_hz": freq_hz,
            "duty": duty, "idle_state": idle_state,
        }))

    def pattern_start(self, bit_idx: int) -> None:
        self.pattern_calls.append(("start", {"bit_idx": bit_idx}))

    def pattern_stop(self, bit_idx: int) -> None:
        self.pattern_calls.append(("stop", {"bit_idx": bit_idx}))

    # --- DIO (DigitalIO) ---

    def dio_set_direction(self, bit_idx: int, output: bool) -> None:
        self.dio_calls.append(("set_direction", {"bit_idx": bit_idx, "output": output}))

    def dio_set(self, bit_idx: int, state: bool) -> None:
        self._dio_pin_values[bit_idx] = state
        self.dio_calls.append(("set", {"bit_idx": bit_idx, "state": state}))

    def dio_read(self, bit_idx: int) -> bool:
        return self._dio_pin_values.get(bit_idx, False)

    def dio_set_voltage(self, volts: float) -> None:
        self._dio_voltage = volts

    def dio_pull_set(self, bit_idx: int, mode: str) -> None:
        if self._open_info is not None and self._open_info.dio_pull_bank_global:
            # Bank-global pull: whole DIO bank set to one mode (mirrors real ADP2230).
            full = (1 << self._open_info.dio_count) - 1
            self.pull_up_mask = full if mode in ("up", "keeper") else 0
            self.pull_down_mask = full if mode in ("down", "keeper") else 0
            return
        m = 1 << bit_idx
        self.pull_up_mask &= ~m
        self.pull_down_mask &= ~m
        if mode == "up":
            self.pull_up_mask |= m
        elif mode == "down":
            self.pull_down_mask |= m
        elif mode == "keeper":  # bus-hold = both masks asserted
            self.pull_up_mask |= m
            self.pull_down_mask |= m

    def din_pull_set(self, mode: str) -> None:
        self.din_pull = mode

    def dio_drive_set(self, bank: int, amps: float, slew: int) -> None:
        self.drive = (bank, amps, slew)

    # --- Logic buffer-mode (DigitalIn) ---

    def logic_configure(
        self, pin_mask: int, sample_rate_hz: float, buffer_size: int
    ) -> None:
        self.logic_calls.append(("configure", {
            "pin_mask": pin_mask, "sample_rate_hz": sample_rate_hz, "buffer_size": buffer_size,
        }))
        self._logic_status_idx = 0
        self.logic_pin_mask = pin_mask
        self.logic_sample_bits = 32 if pin_mask >> 16 else 16

    def logic_set_trigger(
        self, source: str, pin_idx: int | None, level: float | None,
        condition: str | None, position_s: float | None, timeout_s: float | None,
    ) -> None:
        self.logic_calls.append(("set_trigger", {
            "source": source, "pin_idx": pin_idx, "level": level,
            "condition": condition, "position_s": position_s, "timeout_s": timeout_s,
        }))

    def logic_arm(self) -> None:
        self.logic_calls.append(("arm", {}))

    def logic_status(self) -> str:
        idx = min(self._logic_status_idx, len(self._logic_status_sequence) - 1)
        result = self._logic_status_sequence[idx]
        self._logic_status_idx += 1
        return result

    def logic_read(self, count: int) -> np.ndarray:
        bits = self.logic_sample_bits
        if len(self._logic_canned_data) >= count:
            data = self._logic_canned_data[:count]
            # Pad or trim columns to match the configured sample width
            if data.shape[1] < bits:
                pad = np.zeros((data.shape[0], bits - data.shape[1]), dtype=np.uint8)
                return np.concatenate([data, pad], axis=1)
            return data[:, :bits]
        return np.zeros((count, bits), dtype=np.uint8)

    # --- Logic record-mode ---

    def logic_record_configure(
        self, pin_mask: int, sample_rate_hz: float, duration_s: float
    ) -> None:
        self.logic_calls.append(("record_configure", {
            "pin_mask": pin_mask, "sample_rate_hz": sample_rate_hz, "duration_s": duration_s,
        }))
        self._logic_record_status_idx = 0
        self.logic_pin_mask = pin_mask
        self.logic_sample_bits = 32 if pin_mask >> 16 else 16

    def logic_record_arm(self) -> None:
        self.logic_calls.append(("record_arm", {}))

    def logic_record_status(self) -> tuple[int, int, int]:
        idx = min(self._logic_record_status_idx, len(self._logic_record_status_sequence) - 1)
        result = self._logic_record_status_sequence[idx]
        self._logic_record_status_idx += 1
        return result

    def logic_record_read(self, count: int) -> np.ndarray:
        bits = self.logic_sample_bits
        if self._logic_record_chunks_queue:
            chunk = self._logic_record_chunks_queue.pop(0)
        else:
            chunk = self._logic_record_canned_chunk[:count]
        # Pad or trim columns to match the configured sample width
        if chunk.shape[1] < bits:
            pad = np.zeros((chunk.shape[0], bits - chunk.shape[1]), dtype=np.uint8)
            return np.concatenate([chunk, pad], axis=1)
        return chunk[:, :bits]

    def logic_record_stop(self) -> None:
        self.logic_calls.append(("record_stop", {}))

    # Test helpers for logic
    def set_logic_status_sequence(self, sequence: list[str]) -> None:
        self._logic_status_sequence = list(sequence)
        self._logic_status_idx = 0

    def set_logic_record_status_sequence(
        self, sequence: list[tuple[int, int, int]]
    ) -> None:
        self._logic_record_status_sequence = list(sequence)
        self._logic_record_status_idx = 0

    def set_logic_record_chunks(self, chunks: list[np.ndarray]) -> None:
        """Configure successive logic_record_read() calls to return chunks from this
        queue in order. When the queue is exhausted, falls back to the legacy
        canned-chunk slice. Use alongside set_logic_record_status_sequence() to
        drive a deterministic multi-chunk capture in tests."""
        self._logic_record_chunks_queue = list(chunks)

    # --- DMM (AnalogIn measurement) ---

    def dmm_configure(self, channel: int, range_v: float, coupling: str, n_averages: int) -> None:
        self.dmm_calls.append(("configure", {
            "channel": channel, "range_v": range_v,
            "coupling": coupling, "n_averages": n_averages,
        }))

    def dmm_arm(self) -> None:
        self.dmm_calls.append(("arm", {}))
        self._dmm_status_idx = 0

    def dmm_status(self) -> str:
        idx = min(self._dmm_status_idx, len(self._dmm_status_sequence) - 1)
        result = self._dmm_status_sequence[idx]
        self._dmm_status_idx += 1
        return result

    def dmm_read(self, channel: int, count: int) -> np.ndarray:
        if channel in self._dmm_canned_data:
            return self._dmm_canned_data[channel][:count]
        return np.full(count, 1.5, dtype=np.float64)

    def dmm_stop(self) -> None:
        self.dmm_calls.append(("stop", {}))

    # Test helpers
    def set_dmm_canned_data(self, channel: int, data: np.ndarray) -> None:
        self._dmm_canned_data[channel] = data

    def set_dmm_status_sequence(self, seq: list[str]) -> None:
        self._dmm_status_sequence = list(seq)
        self._dmm_status_idx = 0

    # --- SPI (ProtocolSPI) ---

    def spi_configure(
        self, clk_idx: int, freq_hz: float, mode: int,
        mosi_idx: int | None, miso_idx: int | None, cs_idx: int | None,
        cs_polarity: str, bit_order: str,
    ) -> None:
        self.spi_calls.append(("configure", {
            "clk_idx": clk_idx, "freq_hz": freq_hz, "mode": mode,
            "mosi_idx": mosi_idx, "miso_idx": miso_idx, "cs_idx": cs_idx,
            "cs_polarity": cs_polarity, "bit_order": bit_order,
        }))

    def spi_transfer(self, data: bytes, assert_cs: bool) -> bytes:
        self.spi_calls.append(("transfer", {"data": data, "assert_cs": assert_cs}))
        if self._spi_canned_rx:
            return self._spi_canned_rx[: len(data)]
        return bytes(len(data))

    def spi_write(self, data: bytes, assert_cs: bool) -> None:
        self.spi_calls.append(("write", {"data": data, "assert_cs": assert_cs}))

    def spi_read(self, length: int, assert_cs: bool) -> bytes:
        self.spi_calls.append(("read", {"length": length, "assert_cs": assert_cs}))
        if self._spi_canned_rx:
            return self._spi_canned_rx[:length]
        return bytes(length)

    # Test helper
    def set_spi_canned_rx(self, data: bytes) -> None:
        self._spi_canned_rx = data

    # --- UART (ProtocolUART) ---

    def uart_configure(
        self, baud_rate: int, tx_idx: int | None, rx_idx: int | None,
        data_bits: int, parity: str, stop_bits: int,
    ) -> None:
        self.uart_calls.append(("configure", {
            "baud_rate": baud_rate, "tx_idx": tx_idx, "rx_idx": rx_idx,
            "data_bits": data_bits, "parity": parity, "stop_bits": stop_bits,
        }))

    def uart_write(self, data: bytes) -> None:
        self.uart_calls.append(("write", {"data": data}))

    def uart_read(self, length: int, timeout_s: float) -> tuple[bytes, bool]:
        self.uart_calls.append(("read", {"length": length, "timeout_s": timeout_s}))
        return self._uart_canned_rx[:length], self._uart_parity_error

    # Test helpers
    def set_uart_canned_rx(self, data: bytes, parity_error: bool = False) -> None:
        self._uart_canned_rx = data
        self._uart_parity_error = parity_error

    # --- CAN (ProtocolCAN) ---

    def can_configure(self, tx_idx: int, rx_idx: int, bit_rate: int) -> None:
        self.can_calls.append(("configure", {
            "tx_idx": tx_idx, "rx_idx": rx_idx, "bit_rate": bit_rate,
        }))

    def can_send(self, id: int, data: bytes, extended: bool) -> None:
        self.can_calls.append(("send", {"id": id, "data": data, "extended": extended}))

    def can_receive(self, timeout_s: float) -> tuple[int | None, bytes, bool, int]:
        self.can_calls.append(("receive", {"timeout_s": timeout_s}))
        return self._can_canned_frame

    # Test helper
    def set_can_canned_frame(
        self, id: int | None, data: bytes, extended: bool, error_count: int
    ) -> None:
        self._can_canned_frame = (id, data, extended, error_count)

    # --- Sniff (stage 4) ---

    def i2c_spy_start(self) -> None:
        self.sniff_calls.append(("i2c_spy_start", {}))
        self._i2c_spy_idx = 0

    def i2c_spy_status(self, max_data_size: int) -> tuple[int, int, list[int], int]:
        self.sniff_calls.append(("i2c_spy_status", {"max_data_size": max_data_size}))
        if self._i2c_spy_idx < len(self._i2c_spy_sequence):
            result = self._i2c_spy_sequence[self._i2c_spy_idx]
            self._i2c_spy_idx += 1
            return result
        return (0, 0, [], 0)  # no new data

    def i2c_spy_stop(self) -> None:
        self.sniff_calls.append(("i2c_spy_stop", {}))

    def uart_sniff(
        self, rx_pin_idx: int, baud: int, data_bits: int, parity: str, stop_bits: int,
        duration_s: float, poll_interval_s: float, polarity: int,
    ) -> list[tuple[float, bytes, bool]]:
        self.sniff_calls.append(("uart_sniff", {"baud": baud, "polarity": polarity}))
        return list(self._uart_sniff_frames)

    def can_sniff(
        self, rx_pin_idx: int, bitrate: int, duration_s: float, poll_interval_s: float,
    ) -> list[tuple[float, int, bytes, bool, int]]:
        self.sniff_calls.append(("can_sniff", {"bitrate": bitrate}))
        return list(self._can_sniff_frames)

    # Test helpers
    def set_i2c_spy_sequence(self, seq: list[tuple[int, int, list[int], int]]) -> None:
        self._i2c_spy_sequence = list(seq)
        self._i2c_spy_idx = 0

    def set_uart_sniff_frames(self, frames: list[tuple[float, bytes, bool]]) -> None:
        self._uart_sniff_frames = list(frames)

    def set_can_sniff_frames(self, frames: list[tuple[float, int, bytes, bool, int]]) -> None:
        self._can_sniff_frames = list(frames)

    # --- Scope record-mode ---

    def scope_record_configure(
        self,
        channels: list[int],
        range_v: float,
        offset_v: float,
        coupling: str,
        sample_rate_hz: float,
        duration_s: float,
    ) -> None:
        self.scope_record_calls.append(("scope_record_configure", {
            "channels": channels, "range_v": range_v, "offset_v": offset_v,
            "coupling": coupling, "sample_rate_hz": sample_rate_hz,
            "duration_s": duration_s,
        }))

    def scope_record_arm(self) -> None:
        self.scope_record_calls.append(("scope_record_arm", {}))

    def scope_record_status(self) -> tuple[int, int, int]:
        self.scope_record_calls.append(("scope_record_status", {}))
        idx = self._scope_record_status_idx
        seq = self._scope_record_status_sequence
        result = seq[min(idx, len(seq) - 1)]
        self._scope_record_status_idx += 1
        return result

    def scope_record_read(self, count: int) -> np.ndarray:
        self.scope_record_calls.append(("scope_record_read", {"count": count}))
        return self._scope_record_canned_chunk[:count].copy()

    def scope_record_stop(self) -> None:
        self.scope_record_calls.append(("scope_record_stop", {}))

    # Test helper
    def set_scope_record_status_sequence(
        self, seq: list[tuple[int, int, int]]
    ) -> None:
        self._scope_record_status_sequence = list(seq)
        self._scope_record_status_idx = 0
