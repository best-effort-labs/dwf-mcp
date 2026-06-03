from __future__ import annotations

from typing import Any

import numpy as np

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

    def scope_set_trigger(self, source: str, channel: int | None, level_v: float,
                          condition: str, position_s: float, timeout_s: float) -> None:
        self.scope_calls.append(("set_trigger", {
            "source": source, "channel": channel, "level_v": level_v,
            "condition": condition, "position_s": position_s, "timeout_s": timeout_s,
        }))

    def scope_arm(self) -> None:
        self.scope_calls.append(("arm", {}))
        self._scope_status_idx = 0

    def scope_status(self) -> str:
        idx = min(self._scope_status_idx, len(self._scope_status_sequence) - 1)
        result = self._scope_status_sequence[idx]
        self._scope_status_idx += 1
        return result

    def scope_read(self, channel: int, count: int) -> np.ndarray[Any, Any]:
        if channel in self._scope_canned:
            return self._scope_canned[channel][:count]
        return np.zeros(count, dtype=np.float64)

    # Test helpers
    def set_scope_canned_data(self, channels: dict[int, np.ndarray[Any, Any]]) -> None:
        self._scope_canned = dict(channels)

    def set_scope_status_sequence(self, sequence: list[str]) -> None:
        self._scope_status_sequence = list(sequence)
        self._scope_status_idx = 0

    def simulate_unplug(self) -> None:
        self._open_info = None

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
