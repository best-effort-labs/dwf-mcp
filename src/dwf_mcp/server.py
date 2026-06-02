from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DwfBackend
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
from dwf_mcp.policy import SafetyPolicy
from dwf_mcp.registry import InstrumentRegistry

log = logging.getLogger(__name__)


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
    """Holds the device, registry, and tool dispatch. Tests call `call_tool` directly;
    production wires this up to the MCP SDK stdio transport in `main()`."""

    def __init__(self, device: DwfDevice, registry: InstrumentRegistry) -> None:
        self.device = device
        self.registry = registry
        self._tools: dict[str, Any] = {
            "waveforms.open": self._tool_open,
            "waveforms.close": self._tool_close,
            "waveforms.status": self._tool_status,
            "waveforms.list_pins": self._tool_list_pins,
        }

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            self.device.tick_idle()
            return await self._tools[name](**args)
        except KeyError:
            raise ValueError(f"unknown tool {name!r}") from None

    async def _tool_open(self, **kwargs: Any) -> dict[str, Any]:
        policy_fields = {
            f: kwargs.pop(f) for f in [
                "supply_max_voltage_pos", "supply_max_voltage_neg", "supply_max_current",
                "awg_max_amplitude", "pattern_voltage", "require_explicit_enable",
            ] if f in kwargs
        }
        if policy_fields:
            self.device.policy = SafetyPolicy(**policy_fields)
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
    return DwfMcpApp(device, registry)


def main() -> None:
    """Stdio MCP transport entry point. Wires DwfMcpApp into the mcp SDK."""
    logging.basicConfig(level=logging.INFO)
    from mcp.server import Server  # imported lazily
    from mcp.server.stdio import stdio_server

    app = build_app()
    server: Server = Server("dwf-mcp")

    @server.list_tools()
    async def _list_tools() -> list[dict[str, Any]]:
        return [{"name": name, "description": ""} for name in app._tools]  # noqa: SLF001

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        return await app.call_tool(name, arguments)

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
