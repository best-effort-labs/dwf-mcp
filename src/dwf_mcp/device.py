from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfDeviceLost
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
        self._info: DeviceInfo | None = None
        self._last_activity: float | None = None
        self._serial_request: str | None = None

    @property
    def is_open(self) -> bool:
        if self._info is None:
            return False
        # If backend dropped out from under us (unplug), reflect that.
        if not self.backend.is_open:
            self._info = None
            self.allocator.clear()
            return False
        return True

    def open(self, serial: str | None = None) -> DeviceInfo:
        if self.is_open:
            return self._info  # type: ignore[return-value]
        info = self.backend.open(serial=serial)
        self._info = info
        self._serial_request = serial
        self.mark_activity()
        return info

    def close(self) -> None:
        self.allocator.clear()
        if self.backend.is_open:
            self.backend.close()
        self._info = None
        self._last_activity = None

    def require_open(self) -> DeviceInfo:
        if not self.is_open:
            raise DwfDeviceLost("device session is not open (closed, unplugged, or idle-expired)")
        self.mark_activity()
        return self._info  # type: ignore[return-value]

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
