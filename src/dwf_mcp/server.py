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
from dwf_mcp.devices.ad3 import (
    AD3_ANALOG_IN_PINS,
    AD3_ANALOG_OUT_PINS,
    AD3_DIO_PINS,
    AD3_RESOURCE_GROUPS,
    AD3_SUPPLY_PINS,
    AD3_TRIGGER_PINS,
)
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.instruments.i2c import I2C
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.instruments.supply import Supply
from dwf_mcp.policy import SafetyPolicy, SafetyViolation
from dwf_mcp.registry import InstrumentRegistry

log = logging.getLogger(__name__)


_ERROR_TYPES: dict[type[Exception], str] = {
    SafetyViolation: "SafetyViolation",
    PinAllocationError: "PinAllocationError",
    DwfDeviceLost: "DwfDeviceLost",
    InstrumentNotConfigured: "InstrumentNotConfigured",
}


def _build_backend(name: str) -> DwfBackend:
    if name == "fake":
        return FakeBackend()
    if name == "pydwf":
        # Imported lazily so unit tests don't require pydwf to import the module.
        from dwf_mcp.backends.pydwf_backend import PydwfBackend
        return PydwfBackend()
    raise ValueError(f"unknown backend {name!r}")


def _all_pins() -> list[str]:
    return [
        *AD3_DIO_PINS, *AD3_ANALOG_IN_PINS, *AD3_ANALOG_OUT_PINS,
        *AD3_SUPPLY_PINS, *AD3_TRIGGER_PINS,
    ]


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

    def _register_meta_tools(self) -> None:
        meta_schema = {"type": "object", "properties": {}}
        for name, handler in [
            ("waveforms.open", self._tool_open),
            ("waveforms.close", self._tool_close),
            ("waveforms.status", self._tool_status),
            ("waveforms.list_pins", self._tool_list_pins),
        ]:
            self._tools[name] = handler
            self._tool_schemas[name] = meta_schema

    def register_instrument(self, cls: type[Instrument]) -> None:
        """Register an instrument class; the instance is created lazily on first tool call.
        Walks cls.tools to register `{instrument.name}.{suffix}` handlers + their schemas."""
        self.registry.register(cls)
        for suffix, (method_name, schema) in cls.tools.items():
            tool_name = f"{cls.name}.{suffix}"
            self._tools[tool_name] = self._make_instrument_handler(cls.name, method_name)
            self._tool_schemas[tool_name] = schema

    def _make_instrument_handler(self, instrument_name: str, method_name: str) -> Any:
        async def handler(**kwargs: Any) -> Any:
            instrument = self._get_or_create_instrument(instrument_name)
            method = getattr(instrument, method_name)
            return method(**kwargs)
        return handler

    def _get_or_create_instrument(self, name: str) -> Instrument:
        if name not in self.instruments:
            cls = self.registry.get_class(name)
            self.instruments[name] = cls(device=self.device, artifacts=self.artifacts)
        return self.instruments[name]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            handler = self._tools[name]
        except KeyError:
            raise ValueError(f"unknown tool {name!r}") from None
        self.device.tick_idle()
        try:
            result = await handler(**args)
            return cast(dict[str, Any], result)
        except tuple(_ERROR_TYPES.keys()) as exc:
            return {
                "error": {
                    "type": _ERROR_TYPES[type(exc)],
                    "message": str(exc),
                    "details": getattr(exc, "details", {}),
                }
            }

    async def _tool_open(self, **kwargs: Any) -> dict[str, Any]:
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
        info = self.device.open(serial=serial)
        return {
            "device": {
                "serial": info.serial,
                "model": info.model,
                "firmware": info.firmware,
                "sample_rate_max_hz": info.sample_rate_max_hz,
                "dio_count": info.dio_count,
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
        self.device.require_open()
        return {
            "all_pins": _all_pins(),
            "claimed": self.device.allocator.claimed_pins(),
            "resource_groups": [
                {"name": g.name, "pins": sorted(g.pins), "exclusive": g.exclusive}
                for g in self.device.allocator.resource_groups
            ],
        }


def build_app(
    backend_name: str | None = None,
    workspace: str | None = None,
    idle_timeout_s: float = 600.0,
) -> DwfMcpApp:
    backend_name = backend_name or os.environ.get("DWF_BACKEND", "pydwf")
    backend = _build_backend(backend_name)
    allocator = PinAllocator(resource_groups=AD3_RESOURCE_GROUPS)
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=allocator,
        workspace=workspace or "",
        idle_timeout_s=idle_timeout_s,
    )
    registry = InstrumentRegistry()
    app = DwfMcpApp(device, registry)
    app.register_instrument(Scope)
    app.register_instrument(Supply)
    app.register_instrument(I2C)
    return app


def main() -> None:
    """Stdio MCP transport entry point. Wires DwfMcpApp into the mcp SDK."""
    logging.basicConfig(level=logging.INFO)
    from mcp.server import Server  # imported lazily
    from mcp.server.stdio import stdio_server

    app = build_app()
    server: Server = Server("dwf-mcp")

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[dict[str, Any]]:
        return [
            {"name": name, "description": "", "inputSchema": app._tool_schemas[name]}  # noqa: SLF001
            for name in app._tools  # noqa: SLF001
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        return await app.call_tool(name, arguments)

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
