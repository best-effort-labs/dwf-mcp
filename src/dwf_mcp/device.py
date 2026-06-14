from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfDeviceLost
from dwf_mcp.devices.inventory import PinInventory, build_inventory
from dwf_mcp.devices.profiles import DeviceProfile, resolve_profile
from dwf_mcp.policy import SafetyPolicy, SafetyViolation

log = logging.getLogger(__name__)


class DwfDevice:
    _workspace: Path
    _workspace_raw: str

    @property
    def workspace(self) -> Path:
        return self._workspace

    @workspace.setter
    def workspace(self, value: Path | str) -> None:
        self._workspace_raw = str(value)
        self._workspace = Path(value) if value else Path(".")

    def __init__(
        self,
        backend: DwfBackend,
        policy: SafetyPolicy,
        allocator: PinAllocator,
        workspace: Path | str,
        idle_timeout_s: float = 600.0,
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.allocator = allocator
        self.workspace = workspace  # property setter sets _workspace and _workspace_raw
        self.idle_timeout_s = idle_timeout_s
        self.vcd_enabled: bool = True
        self._info: DeviceInfo | None = None
        self._last_activity: float | None = None
        self._serial_request: str | None = None
        self._config_request: str | None = None
        self.profile: DeviceProfile | None = None
        self.inventory: PinInventory | None = None
        self.on_close: Callable[[], None] | None = None
        self.current_dio_voltage: float = 3.3

    @property
    def is_open(self) -> bool:
        if self._info is None:
            return False
        # If backend dropped out from under us (unplug), reflect that.
        if not self.backend.is_open:
            self.close()
            return False
        return True

    def open(self, serial: str | None = None, device_config: str | None = None) -> DeviceInfo:
        if self.is_open:
            return self._info  # type: ignore[return-value]
        info = self.backend.open(serial=serial, device_config=device_config)
        try:
            self.profile = resolve_profile(info.devid)
            self.inventory = build_inventory(self.profile, info)
            self.allocator.configure(
                known_pins=self.inventory.all_known(),
                resource_groups=self.profile.build_resource_groups(
                    analog_in_channels=info.analog_in_channels,
                    user_awg_count=self.profile.user_awg_count,
                ),
            )
        except Exception:
            self.profile = None
            self.inventory = None
            self.allocator.reset_configuration()
            with contextlib.suppress(Exception):
                self.backend.close()
            raise
        self._info = info
        if self.profile is not None and self.profile.dio_voltage_range is not None:
            lo, hi = self.profile.dio_voltage_range
            self.current_dio_voltage = hi  # DD powers up at its max rail
        self._serial_request = serial
        self._config_request = device_config
        self.mark_activity()
        return info

    def close(self) -> None:
        self.allocator.reset_configuration()
        if self.backend.is_open:
            self.backend.close()
        self._info = None
        self.profile = None
        self.inventory = None
        self._last_activity = None
        if self.on_close is not None:
            self.on_close()

    def require_open(self) -> DeviceInfo:
        if not self.is_open:
            raise DwfDeviceLost("device session is not open (closed, unplugged, or idle-expired)")
        self.mark_activity()
        return self._info  # type: ignore[return-value]

    def validate_pin(self, pin: str) -> None:
        if self.inventory is None or not self.inventory.is_valid_pin(pin):
            raise ValueError(f"pin {pin!r} is not available on {self._device_name()}")

    def validate_channel(self, channel: int, kind: str) -> None:
        assert self._info is not None
        count = (self._info.analog_in_channels if kind == "scope"
                 else self.profile.user_awg_count if self.profile else 0)
        if not (1 <= channel <= count):
            raise ValueError(
                f"{kind} channel {channel} out of range 1..{count} on {self._device_name()}"
            )

    def validate_rate(self, rate_hz: float) -> None:
        assert self._info is not None
        if rate_hz > self._info.sample_rate_max_hz:
            raise ValueError(
                f"sample_rate_hz {rate_hz} exceeds device max "
                f"{self._info.sample_rate_max_hz} on {self._device_name()}"
            )

    def validate_output_pin(self, pin: str) -> None:
        self.validate_pin(pin)
        if self.inventory is not None and pin in self.inventory.input_only:
            raise ValueError(f"pin {pin!r} is input-only on {self._device_name()}")

    def validate_input_pin(self, pin: str) -> None:
        self.validate_pin(pin)

    def validate_logic_rate(self, rate_hz: float) -> None:
        assert self._info is not None
        cap = self._info.digital_in_rate_max_hz
        if cap > 0 and rate_hz > cap:
            raise ValueError(
                f"logic sample_rate_hz {rate_hz} exceeds digital max {cap} "
                f"on {self._device_name()}"
            )

    def validate_awg_samples(self, n_samples: int) -> None:
        """Reject a custom AWG waveform that exceeds the device's AnalogOut buffer.
        Skipped when the buffer size is unknown (0)."""
        assert self._info is not None
        cap = self._info.analog_out_buffer_max
        if cap > 0 and n_samples > cap:
            raise ValueError(
                f"custom waveform has {n_samples} samples, exceeds the AnalogOut "
                f"buffer ({cap}) on {self._device_name()}"
            )

    def validate_supply_voltage(self, channel: str, voltage: float) -> None:
        """On devices with fixed (non-programmable) supplies, reject any voltage
        other than the rail's fixed value. No-op for programmable supplies."""
        fixed = self.profile.fixed_supply_voltages if self.profile else None
        if fixed is not None and channel in fixed:
            expected = fixed[channel]
            if abs(voltage - expected) > 1e-6:
                raise ValueError(
                    f"supply rail {channel!r} on {self._device_name()} is fixed at "
                    f"{expected} V; cannot set {voltage} V"
                )

    def _device_name(self) -> str:
        return self.profile.name if self.profile else "no device"

    def mark_activity(self) -> None:
        self._last_activity = time.monotonic()

    def tick_idle(self) -> None:
        if self._info is None or self._last_activity is None:
            return
        if time.monotonic() - self._last_activity >= self.idle_timeout_s:
            self.close()

    def gate_output(self, kind: str, **params: Any) -> None:
        """Safety gate for any 'output goes hot' path. Checks policy, writes the safety
        log, raises SafetyViolation on rejection. Rejected attempts are logged too."""
        rejected = False
        rejection_reason: str | None = None
        try:
            self._check_policy(kind, **params)
        except SafetyViolation as exc:
            rejected = True
            rejection_reason = str(exc)
            raise
        finally:
            self._append_safety_log(
                kind=kind, params=params, rejected=rejected, reason=rejection_reason
            )

    def _check_policy(self, kind: str, **params: Any) -> None:
        if kind == "supply_enable":
            channel = params.get("channel")
            voltage = params.get("voltage")
            if not isinstance(channel, str):
                raise SafetyViolation(
                    f"supply_enable requires str channel, got {type(channel).__name__}"
                )
            if not isinstance(voltage, int | float):
                raise SafetyViolation(
                    f"supply_enable requires numeric voltage, got {type(voltage).__name__}"
                )
            self.policy.check_supply_voltage(channel, float(voltage))
            current_limit = params.get("current_limit")
            if current_limit is not None:
                if not isinstance(current_limit, int | float):
                    raise SafetyViolation(
                        f"supply_enable current_limit must be numeric if given, "
                        f"got {type(current_limit).__name__}"
                    )
                self.policy.check_supply_current(float(current_limit))
        elif kind == "awg_start":
            amplitude = params.get("amplitude")
            if not isinstance(amplitude, int | float):
                raise SafetyViolation(
                    f"awg_start requires numeric amplitude, got {type(amplitude).__name__}"
                )
            self.policy.check_awg_amplitude(float(amplitude))
        elif kind == "pattern_start":
            self.policy.check_pattern_voltage()
        elif kind == "dio_set":
            # DIO output is the same fixed-3.3 V hardware as the pattern generator.
            self.policy.check_pattern_voltage()
        # Unknown kinds pass through (forward-compat for stage 3 kinds).

    def _append_safety_log(
        self, kind: str, params: dict[str, Any], rejected: bool, reason: str | None
    ) -> None:
        try:
            if self._workspace_raw == "":
                log.info(
                    "safety event (no workspace): kind=%s params=%s rejected=%s reason=%s",
                    kind, params, rejected, reason,
                )
                return
            path = self.workspace / "dwf-safety.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(UTC).isoformat(),
                "kind": kind,
                "params": params,
                "rejected": rejected,
                "reason": reason,
            }
            with path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            log.exception("failed to write safety log entry for kind=%r", kind)

    def status(self) -> dict[str, Any]:
        idle_remaining: float | None = None
        if self._last_activity is not None:
            elapsed = time.monotonic() - self._last_activity
            idle_remaining = max(0.0, self.idle_timeout_s - elapsed)
        info = None
        if self._info is not None:
            info = {
                "serial": self._info.serial,
                "model": self._info.model,
                "firmware": self._info.firmware,
                "devid": self._info.devid,
                "dio_count": self._info.dio_count,
                "sample_rate_max_hz": self._info.sample_rate_max_hz,
            }
        return {
            "open": self.is_open,
            "device": info,
            "workspace": str(self.workspace),
            "claimed_pins": self.allocator.claimed_pins(),
            "claimed_instruments": self.allocator.claimed_instruments(),
            "idle_remaining_s": idle_remaining,
            "policy": {
                "supply_max_voltage_pos": self.policy.supply_max_voltage_pos,
                "supply_max_voltage_neg": self.policy.supply_max_voltage_neg,
                "supply_max_current": self.policy.supply_max_current,
                "awg_max_amplitude": self.policy.awg_max_amplitude,
                "pattern_voltage": self.policy.pattern_voltage,
                "require_explicit_enable": self.policy.require_explicit_enable,
            },
        }
