"""Verify that _make_instrument_handler awaits coroutine methods."""
from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from dwf_mcp.instrument import Instrument
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice


class _AsyncInstrument(Instrument):
    name = "async_test"
    tools: ClassVar[dict[str, Any]] = {
        "do_async": ("do_async", {"type": "object", "properties": {}}),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        pass

    async def do_async(self) -> dict[str, Any]:
        return {"async": True}

    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_handler_awaits_coroutine(tmp_path):
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.registry.register(_AsyncInstrument)
    app._tools["async_test.do_async"] = app._make_instrument_handler("async_test", "do_async")
    app.instruments["async_test"] = _AsyncInstrument(device=app.device, artifacts=app.artifacts)
    result = await app.call_tool("async_test.do_async", {})
    assert result == {"async": True}


def test_build_app_registers_stage3a_tools(tmp_path):
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    tool_names = set(app._tools)
    expected = {
        "awg.configure", "awg.upload_custom", "awg.start", "awg.stop",
        "pattern.configure", "pattern.start", "pattern.stop",
        "dio.set_direction", "dio.set", "dio.read",
        "logic.configure", "logic.set_trigger", "logic.capture",
        "logic.record_start", "logic.record_status", "logic.record_stop",
    }
    missing = expected - tool_names
    assert missing == set(), f"missing tools: {missing}"


def test_build_app_registers_stage3b_tools(tmp_path):
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    tool_names = set(app._tools)
    expected = {
        "dmm.measure",
        "spi.configure", "spi.transfer", "spi.write", "spi.read",
        "uart.configure", "uart.write", "uart.read",
        "can.configure", "can.send", "can.receive",
    }
    missing = expected - tool_names
    assert missing == set(), f"missing tools: {missing}"
