from __future__ import annotations

from typing import Any, ClassVar

import pytest

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.policy import SafetyViolation
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


class _Echo(Instrument):
    """Test instrument that exposes a few tool methods covering success and each error kind."""
    name = "echo"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "ping": ("ping", {"type": "object", "properties": {}}),
        "boom_safety": ("boom_safety", {"type": "object", "properties": {}}),
        "boom_unconfigured": ("boom_unconfigured", {"type": "object", "properties": {}}),
    }

    def __init__(self, device: object, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts

    def ping(self) -> dict[str, str]:
        return {"pong": "ok"}

    def boom_safety(self) -> dict[str, Any]:
        raise SafetyViolation("over-voltage")

    def boom_unconfigured(self) -> dict[str, Any]:
        raise InstrumentNotConfigured("must configure first")

    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_register_instrument_dispatches_to_method(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("echo.ping", {})
    assert result == {"pong": "ok"}


@pytest.mark.asyncio
async def test_register_instrument_lazy_instantiation(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    assert "echo" not in app.instruments  # not yet created
    await app.call_tool("waveforms.open", {})
    await app.call_tool("echo.ping", {})
    assert "echo" in app.instruments


@pytest.mark.asyncio
async def test_safety_violation_maps_to_error_result(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("echo.boom_safety", {})
    assert result["error"]["type"] == "SafetyViolation"
    assert "over-voltage" in result["error"]["message"]


@pytest.mark.asyncio
async def test_instrument_not_configured_maps_to_error_result(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("echo.boom_unconfigured", {})
    assert result["error"]["type"] == "InstrumentNotConfigured"


@pytest.mark.asyncio
async def test_open_with_workspace_dir_rebuilds_artifacts(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace="")  # default tempdir
    await app.call_tool("waveforms.open", {"workspace_dir": str(tmp_path)})
    assert str(app.artifacts.workspace) == str(tmp_path)
    assert str(app.device.workspace) == str(tmp_path)


@pytest.mark.asyncio
async def test_release_called_on_close(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    await app.call_tool("echo.ping", {})  # instantiates echo
    echo = app.instruments["echo"]
    released = {"called": False}
    echo.release = lambda: released.update(called=True)  # type: ignore[method-assign]
    await app.call_tool("waveforms.close", {})
    assert released["called"] is True
    assert app.instruments == {}
