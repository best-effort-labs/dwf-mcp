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
