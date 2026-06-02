from __future__ import annotations

import pytest

from dwf_mcp.server import build_app


@pytest.mark.asyncio
async def test_open_then_status_then_close(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))

    open_result = await app.call_tool("waveforms.open", {})
    assert open_result["device"]["serial"] == "FAKE-AD3-0001"

    status = await app.call_tool("waveforms.status", {})
    assert status["open"] is True

    close_result = await app.call_tool("waveforms.close", {})
    assert close_result["closed"] is True

    status = await app.call_tool("waveforms.status", {})
    assert status["open"] is False


@pytest.mark.asyncio
async def test_list_pins_reflects_claims(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    app.device.allocator.claim("test", ["dio0", "dio1"])
    pins = await app.call_tool("waveforms.list_pins", {})
    assert pins["claimed"] == {"dio0": "test", "dio1": "test"}
    assert "dio0" in pins["all_pins"]


@pytest.mark.asyncio
async def test_open_accepts_safety_policy_kwargs(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {"supply_max_voltage_pos": 1.8})
    status = await app.call_tool("waveforms.status", {})
    assert status["policy"]["supply_max_voltage_pos"] == 1.8
