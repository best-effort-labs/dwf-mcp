"""Supply (AnalogIO) instrument. Safety-gated programmable rails: vpos / vneg."""
from __future__ import annotations

import contextlib
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

# Alias because this module defines a method named `set` on Supply, which shadows
# the builtin in type annotations resolved from the class body (mypy issue).
_Set = set

_CHANNEL_TO_POLICY_KIND = {"vpos": "pos", "vneg": "neg"}

SUPPLY_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "voltage"],
    "properties": {
        "channel": {"type": "string", "enum": ["vpos", "vneg"]},
        "voltage": {"type": "number"},
        "current_limit": {"type": "number", "minimum": 0.0},
    },
}

SUPPLY_CHANNEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel"],
    "properties": {"channel": {"type": "string", "enum": ["vpos", "vneg"]}},
}


class Supply(Instrument):
    name = "supply"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "set":     ("set",     SUPPLY_SET_SCHEMA),
        "enable":  ("enable",  SUPPLY_CHANNEL_SCHEMA),
        "disable": ("disable", SUPPLY_CHANNEL_SCHEMA),
        "read":    ("read",    SUPPLY_CHANNEL_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._layout = device.backend.supply_discover_nodes()
        self._setpoints: dict[str, dict[str, float]] = {}  # channel -> {voltage, current_limit}
        self._enabled: _Set[str] = set()

    def set(
        self, channel: str, voltage: float, current_limit: float | None = None
    ) -> dict[str, Any]:
        if channel not in self._layout:
            raise ValueError(
                f"unknown supply channel {channel!r}; have {sorted(self._layout)}"
            )
        ch_idx, nodes = self._layout[channel]
        # Apply Scope's partial-failure pattern: claim pins, then clear stale state,
        # then try backend calls, then commit state on success.
        prior_setpoint = self._setpoints.get(channel)
        new_claims = sorted(self._claimed_channels() | {channel})
        self.device.allocator.claim("supply", new_claims)
        self._setpoints.pop(channel, None)
        try:
            self.device.backend.supply_node_set(ch_idx, nodes["voltage"], voltage)
            if current_limit is not None:
                self.device.backend.supply_node_set(ch_idx, nodes["current"], current_limit)
        except Exception:
            # On backend failure: revert claim list and restore prior setpoint.
            self.device.allocator.claim("supply", sorted(self._claimed_channels()))
            if prior_setpoint is not None:
                self._setpoints[channel] = prior_setpoint
            raise
        resolved_current = (
            current_limit if current_limit is not None
            else (prior_setpoint or {}).get("current_limit", 0.0)
        )
        self._setpoints[channel] = {"voltage": voltage, "current_limit": resolved_current}
        return {"set": True, "channel": channel, "voltage": voltage, "current_limit": current_limit}

    def enable(self, channel: str) -> dict[str, Any]:
        if channel not in self._setpoints:
            raise InstrumentNotConfigured(
                f"supply.set must be called for {channel!r} before enable"
            )
        ch_idx, nodes = self._layout[channel]
        sp = self._setpoints[channel]
        # Safety gate — raises SafetyViolation on rejection (also logs).
        self.device.gate_output(
            "supply_enable",
            channel=_CHANNEL_TO_POLICY_KIND[channel],
            voltage=sp["voltage"],
            current_limit=sp["current_limit"],
        )
        self.device.backend.supply_node_set(ch_idx, nodes["enable"], 1.0)
        self._enabled.add(channel)
        self.device.backend.supply_master_enable(True)
        return {"enabled": True, "channel": channel}

    def disable(self, channel: str) -> dict[str, Any]:
        if channel not in self._layout:
            raise ValueError(f"unknown supply channel {channel!r}")
        ch_idx, nodes = self._layout[channel]
        self.device.backend.supply_node_set(ch_idx, nodes["enable"], 0.0)
        self._enabled.discard(channel)
        if not self._enabled:
            self.device.backend.supply_master_enable(False)
        return {"disabled": True, "channel": channel}

    def read(self, channel: str) -> dict[str, Any]:
        if channel not in self._layout:
            raise ValueError(f"unknown supply channel {channel!r}")
        ch_idx, nodes = self._layout[channel]
        measured_v = self.device.backend.supply_node_get(ch_idx, nodes["voltage"])
        measured_i = self.device.backend.supply_node_get(ch_idx, nodes["current"])
        requested = self._setpoints.get(channel, {"voltage": 0.0, "current_limit": 0.0})
        return {
            "channel": channel,
            "requested": requested,
            "measured": {"voltage": measured_v, "current": measured_i},
            "enabled": channel in self._enabled,
        }

    def release(self) -> None:
        for ch in list(self._enabled):
            with contextlib.suppress(Exception):
                self.disable(ch)
        self.device.allocator.release("supply")
        self._setpoints.clear()
        self._enabled.clear()

    def _claimed_channels(self) -> _Set[str]:
        claims = self.device.allocator.claimed_pins()
        return {pin for pin, instr in claims.items() if instr == "supply"}
