from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, cast

from dwf_mcp.allocator import PinAllocationError, PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backend import DwfBackend, DwfDeviceLost
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.configs import CONFIG_STRATEGIES
from dwf_mcp.devices.profiles import UnsupportedDeviceError
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.instruments.awg import AWG
from dwf_mcp.instruments.can import CAN
from dwf_mcp.instruments.decoder import Decoder as DecoderInstrument
from dwf_mcp.instruments.dio import DIO
from dwf_mcp.instruments.dmm import DMM
from dwf_mcp.instruments.i2c import I2C
from dwf_mcp.instruments.logic import Logic
from dwf_mcp.instruments.pattern import Pattern
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.instruments.sniff import Sniff
from dwf_mcp.instruments.spi import SPI
from dwf_mcp.instruments.supply import Supply
from dwf_mcp.instruments.uart import UART
from dwf_mcp.policy import SafetyPolicy, SafetyViolation
from dwf_mcp.registry import InstrumentRegistry

log = logging.getLogger(__name__)


_ERROR_TYPES: dict[type[Exception], str] = {
    SafetyViolation: "SafetyViolation",
    PinAllocationError: "PinAllocationError",
    DwfDeviceLost: "DwfDeviceLost",
    InstrumentNotConfigured: "InstrumentNotConfigured",
    # Open-time validation failure (unknown devid) — surface cleanly rather than
    # letting the generic handler mislabel it as DwfDeviceLost after open cleanup.
    UnsupportedDeviceError: "UnsupportedDeviceError",
}

# Tools that manage or report device lifecycle — they must run regardless of
# whether a device is currently open, so they skip the pre-dispatch require_open.
_LIFECYCLE_TOOLS = frozenset({"waveforms.open", "waveforms.close", "waveforms.status"})


def _build_backend(name: str) -> DwfBackend:
    if name == "fake":
        return FakeBackend()
    if name == "pydwf":
        # Imported lazily so unit tests don't require pydwf to import the module.
        from dwf_mcp.backends.pydwf_backend import PydwfBackend
        return PydwfBackend()
    raise ValueError(f"unknown backend {name!r}")


def _all_pins(device: DwfDevice) -> list[str]:
    if device.inventory is None:
        return []
    return device.inventory.all_physical_pins()


class DwfMcpApp:
    """Holds the device, registry, instruments, and tool dispatch. Tests call `call_tool`
    directly; production wires this up to the MCP SDK stdio transport in `main()`."""

    def __init__(self, device: DwfDevice, registry: InstrumentRegistry) -> None:
        self.device = device
        self.registry = registry
        self.instruments: dict[str, Instrument] = {}
        self.artifacts = ArtifactWriter(
            workspace=device.workspace if str(device.workspace) else None
        )
        # Sync device.workspace to whatever ArtifactWriter resolved (covers the temp-dir fallback).
        self.device.workspace = self.artifacts.workspace
        self._tools: dict[str, Any] = {}
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        self._register_meta_tools()
        self.device.on_close = self._on_device_close

    def _on_device_close(self) -> None:
        self.instruments.clear()

    def _register_meta_tools(self) -> None:
        meta_schema = {"type": "object", "properties": {}}
        open_schema = {
            "type": "object",
            "properties": {
                "device_serial": {
                    "type": "string",
                    "description": "Open a specific device by serial; omit to use the first found.",
                },
                "device_config": {
                    "type": "string",
                    "enum": list(CONFIG_STRATEGIES),
                    "description": (
                        "Hardware configuration strategy. On shared-IO devices "
                        "(Analog Discovery 1/2) buffers trade off, so choose by intent: "
                        "'max_digital_in' before high-rate logic/protocol sniffing, "
                        "'max_analog_in' before long analog records, else 'default'. "
                        "Changing it later requires a close + re-open."
                    ),
                },
                "workspace_dir": {"type": "string"},
            },
        }
        for name, handler, schema in [
            ("waveforms.open", self._tool_open, open_schema),
            ("waveforms.close", self._tool_close, meta_schema),
            ("waveforms.status", self._tool_status, meta_schema),
            ("waveforms.list_pins", self._tool_list_pins, meta_schema),
        ]:
            self._tools[name] = self._wrap_meta_handler(handler)
            self._tool_schemas[name] = schema

    @staticmethod
    def _wrap_meta_handler(handler: Any) -> Any:
        """Wrap a meta-tool handler so it silently ignores on_record_chunk."""
        async def wrapper(on_record_chunk: Any = None, **kwargs: Any) -> Any:
            return await handler(**kwargs)
        return wrapper

    def register_instrument(self, cls: type[Instrument]) -> None:
        """Register an instrument class; the instance is created lazily on first tool call.
        Walks cls.tools to register `{instrument.name}.{suffix}` handlers + their schemas."""
        self.registry.register(cls)
        for suffix, (method_name, schema) in cls.tools.items():
            tool_name = f"{cls.name}.{suffix}"
            self._tools[tool_name] = self._make_instrument_handler(cls.name, method_name)
            self._tool_schemas[tool_name] = schema

    def _make_instrument_handler(self, instrument_name: str, method_name: str) -> Any:
        async def handler(
            on_record_chunk: Any = None,
            **kwargs: Any,
        ) -> Any:
            instrument = self._get_or_create_instrument(instrument_name)
            method = getattr(instrument, method_name)
            if method_name == "record_start" and on_record_chunk is not None:
                kwargs["on_chunk"] = on_record_chunk
            result = method(**kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result
        return handler

    def _get_or_create_instrument(self, name: str) -> Instrument:
        if name not in self.instruments:
            # Construction may touch hardware (e.g. Supply discovers nodes); ensure
            # the device is open so we surface a clean DwfDeviceLost rather than a
            # backend exception escaping from __init__.
            self.device.require_open()
            if (self.device.profile is not None
                    and name not in self.device.profile.supported_instruments):
                raise InstrumentNotConfigured(
                    f"instrument {name!r} is not available on this device "
                    f"({self.device.profile.name})"
                )
            cls = self.registry.get_class(name)
            self.instruments[name] = cls(device=self.device, artifacts=self.artifacts)
        return self.instruments[name]

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        on_record_chunk: Any = None,
    ) -> dict[str, Any]:
        try:
            handler = self._tools[name]
        except KeyError:
            raise ValueError(f"unknown tool {name!r}") from None
        # Detect a closed/unplugged/idle-expired device before dispatching an
        # instrument tool, so it returns a clean DwfDeviceLost instead of a raw
        # backend error. is_open probes the live link; on loss DwfDevice clears
        # its info + allocator, and we drop instrument state here. Lifecycle tools
        # (open/close/status) must run regardless of device state.
        if name not in _LIFECYCLE_TOOLS:
            try:
                self.device.require_open()
            except DwfDeviceLost as exc:
                self._reset_after_device_lost()
                return self._error_payload(exc)
        # Automatic idle reaping between tool calls is armed only for a positive
        # timeout; idle_timeout_s <= 0 disables auto-close (an explicit
        # device.tick_idle() still evaluates the configured timeout directly).
        if self.device.idle_timeout_s > 0:
            self.device.tick_idle()
        # Give every live instrument a chance to reap idle/background state (e.g.
        # orphan sniff sessions) regardless of which tool was called.
        for instrument in list(self.instruments.values()):
            instrument.tick_idle()
        try:
            result = await handler(on_record_chunk=on_record_chunk, **args)
            return cast(dict[str, Any], result)
        except tuple(_ERROR_TYPES.keys()) as exc:
            if isinstance(exc, DwfDeviceLost):
                self._reset_after_device_lost()
            return self._error_payload(exc)
        except Exception:
            # A handler raised something unexpected. If the device vanished
            # mid-call, surface it as a clean DwfDeviceLost and reset; otherwise
            # it's a genuine error — re-raise it unchanged (no false teardown).
            if not self.device.is_open:
                self._reset_after_device_lost()
                return self._error_payload(
                    DwfDeviceLost("device lost during operation (unplug or communication failure)")
                )
            raise

    def _reset_after_device_lost(self) -> None:
        """Drop server-side instrument state after the device disappears. The
        cached device info and allocator claims are already cleared by
        DwfDevice.is_open; this clears the instrument cache so a re-open is clean."""
        self.instruments.clear()

    @staticmethod
    def _error_payload(exc: Exception) -> dict[str, Any]:
        return {
            "error": {
                "type": _ERROR_TYPES.get(type(exc), type(exc).__name__),
                "message": str(exc),
                "details": getattr(exc, "details", {}),
            }
        }

    async def _tool_open(self, **kwargs: Any) -> dict[str, Any]:
        requested_serial = kwargs.get("device_serial")
        requested_config = kwargs.get("device_config")
        # Validate the strategy up front so a bad value is a clean error, not a
        # deep ValueError mislabeled as device-lost during open.
        if requested_config is not None and requested_config not in CONFIG_STRATEGIES:
            return self._error_payload(ValueError(
                f"unknown device_config {requested_config!r}; expected one of {CONFIG_STRATEGIES}"
            ))
        if self.device.is_open:
            current_serial = self.device._info.serial if self.device._info else None
            if requested_serial not in (None, current_serial):
                return self._error_payload(DwfDeviceLost(
                    "a device is already open; close it before opening a different serial"
                ))
            # device_config is a hardware choice latched at open; a reopen is
            # idempotent and cannot change it, so reject a conflicting request
            # rather than silently returning the device on the old config.
            def _norm(c: str | None) -> str | None:
                return None if c in (None, "default") else c
            if (requested_config is not None
                    and _norm(requested_config) != _norm(self.device._config_request)):
                return self._error_payload(DwfDeviceLost(
                    "a device is already open; close it before changing device_config"
                ))
        policy_fields = {
            f: kwargs.pop(f) for f in [
                "supply_max_voltage_pos", "supply_max_voltage_neg", "supply_max_current",
                "awg_max_amplitude", "pattern_voltage", "require_explicit_enable",
            ] if f in kwargs
        }
        if policy_fields:
            self.device.policy = SafetyPolicy(**policy_fields)
        workspace_dir = kwargs.pop("workspace_dir", None)
        if workspace_dir:
            self.device.workspace = Path(workspace_dir)
            self.artifacts = ArtifactWriter(workspace=self.device.workspace)
        serial = kwargs.pop("device_serial", None)
        device_config = kwargs.pop("device_config", None)
        info = self.device.open(serial=serial, device_config=device_config)
        return {
            "device": {
                "serial": info.serial,
                "model": info.model,
                "firmware": info.firmware,
                "sample_rate_max_hz": info.sample_rate_max_hz,
                "dio_count": info.dio_count,
                "digital_in_buffer_max": info.digital_in_buffer_max,
                "analog_in_buffer_max": info.analog_in_buffer_max,
            },
            "workspace": str(self.device.workspace),
        }

    async def _tool_close(self) -> dict[str, Any]:
        for instrument in list(self.instruments.values()):
            instrument.release()
        self.instruments.clear()
        self.device.close()
        return {"closed": True}

    async def _tool_status(self) -> dict[str, Any]:
        return self.device.status()

    async def _tool_list_pins(self) -> dict[str, Any]:
        info = self.device.require_open()
        assert self.device.profile is not None  # guaranteed once the device is open
        return {
            "all_pins": _all_pins(self.device),
            "claimed": self.device.allocator.claimed_pins(),
            "resource_groups": [
                {"name": g.name, "pins": sorted(g.pins), "exclusive": g.exclusive}
                for g in self.device.allocator.resource_groups
            ],
            "limits": {
                "dio_count": info.dio_count,
                "analog_in_channels": info.analog_in_channels,
                "user_awg_channels": self.device.profile.user_awg_count,
                "sample_rate_max_hz": info.sample_rate_max_hz,
            },
        }


def build_app(
    backend_name: str | None = None,
    workspace: str | None = None,
    idle_timeout_s: float = 600.0,
    enable_vcd: bool | None = None,
) -> DwfMcpApp:
    backend_name = backend_name or os.environ.get("DWF_BACKEND", "pydwf")
    workspace = workspace or os.environ.get("DWF_WORKSPACE")
    backend = _build_backend(backend_name)
    allocator = PinAllocator()  # resource groups configured at device open
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=allocator,
        workspace=workspace or "",
        idle_timeout_s=idle_timeout_s,
    )
    if enable_vcd is True:
        from dwf_mcp import vcd_writer as _vw
        if not _vw.HAS_VCD:
            raise ImportError(
                "enable_vcd=True but pyvcd is not installed: pip install dwf-mcp[vcd]"
            )
        device.vcd_enabled = True
    elif enable_vcd is False:
        device.vcd_enabled = False
    else:
        from dwf_mcp import vcd_writer as _vw
        device.vcd_enabled = _vw.HAS_VCD
    registry = InstrumentRegistry()
    app = DwfMcpApp(device, registry)
    app.register_instrument(Scope)
    app.register_instrument(Supply)
    app.register_instrument(I2C)
    app.register_instrument(AWG)
    app.register_instrument(Pattern)
    app.register_instrument(DIO)
    app.register_instrument(Logic)
    app.register_instrument(DMM)
    app.register_instrument(SPI)
    app.register_instrument(UART)
    app.register_instrument(CAN)
    app.register_instrument(Sniff)
    app.register_instrument(DecoderInstrument)
    return app


def main() -> None:
    """Stdio MCP transport entry point. Wires DwfMcpApp into the mcp SDK."""
    import base64
    import json as _json
    logging.basicConfig(level=logging.INFO)
    from mcp.server import Server  # imported lazily
    from mcp.server.stdio import stdio_server

    _vcd_env = os.environ.get("DWF_ENABLE_VCD")
    _enable_vcd: bool | None = None
    if _vcd_env == "1":
        _enable_vcd = True
    elif _vcd_env == "0":
        _enable_vcd = False
    app = build_app(enable_vcd=_enable_vcd)
    server: Server = Server("dwf-mcp")

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[dict[str, Any]]:
        return [
            {"name": name, "description": "", "inputSchema": app._tool_schemas[name]}  # noqa: SLF001
            for name in app._tools  # noqa: SLF001
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        mcp_session = server.request_context.session

        async def on_chunk(record_id: str, chunk: Any) -> None:
            import numpy as np
            arr = np.asarray(chunk)
            await mcp_session.send_log_message(
                level="info",
                data=_json.dumps({
                    "event": "record_chunk",
                    "record_id": record_id,
                    "n_samples": int(arr.shape[0]),
                    "dtype": str(arr.dtype),
                    "shape": list(arr.shape),
                    "data_b64": base64.b64encode(arr.tobytes()).decode(),
                }),
            )

        return await app.call_tool(name, arguments, on_record_chunk=on_chunk)

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
