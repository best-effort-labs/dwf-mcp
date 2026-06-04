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


@pytest.mark.asyncio
async def test_on_record_chunk_injected_for_logic_record_start(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.open()

    chunks_seen: list[Any] = []
    async def on_chunk(record_id: str, chunk: Any) -> None:
        chunks_seen.append((record_id, chunk))

    result = await app.call_tool(
        "logic.record_start",
        {"pins": ["dio0"], "sample_rate_hz": 100.0, "duration_s": 0.1},
        on_record_chunk=on_chunk,
    )
    assert "record_id" in result
    record_id = result["record_id"]

    await asyncio.sleep(0.05)

    stop = await app.call_tool("logic.record_stop", {"record_id": record_id})
    assert stop.get("error") is None


@pytest.mark.asyncio
async def test_on_record_chunk_not_injected_for_non_record_start(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.open()

    injected: list[Any] = []
    async def on_chunk(record_id: str, chunk: Any) -> None:
        injected.append(chunk)

    result = await app.call_tool(
        "logic.configure",
        {"pins": ["dio0"], "sample_rate_hz": 1_000_000.0, "buffer_size": 1024},
        on_record_chunk=on_chunk,
    )
    assert result.get("configured") is True
    assert injected == []


@pytest.mark.asyncio
async def test_call_tool_without_on_record_chunk_still_works(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.open()

    result = await app.call_tool(
        "logic.record_start",
        {"pins": ["dio0"], "sample_rate_hz": 100.0, "duration_s": 0.1},
    )
    assert "record_id" in result
    record_id = result["record_id"]
    await asyncio.sleep(0.05)
    stop = await app.call_tool("logic.record_stop", {"record_id": record_id})
    assert stop.get("error") is None


def test_build_app_registers_stage3b_streaming_tools(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    tool_names = set(app._tools)
    expected = {
        "scope.record_start", "scope.record_status", "scope.record_stop",
    }
    missing = expected - tool_names
    assert missing == set(), f"missing tools: {missing}"
