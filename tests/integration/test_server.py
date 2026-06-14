from __future__ import annotations

import dataclasses
from typing import Any, ClassVar

import numpy as np
import pytest

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.policy import SafetyViolation
from dwf_mcp.server import build_app


def _allow_echo(app: Any) -> None:
    """Add the test-only ``echo`` instrument to the open device's supported set so
    the server's supported-instrument gate doesn't reject it. Call after open."""
    app.device.profile = dataclasses.replace(
        app.device.profile,
        supported_instruments=app.device.profile.supported_instruments | {"echo"},
    )


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
    _allow_echo(app)
    result = await app.call_tool("echo.ping", {})
    assert result == {"pong": "ok"}


@pytest.mark.asyncio
async def test_register_instrument_lazy_instantiation(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    assert "echo" not in app.instruments  # not yet created
    await app.call_tool("waveforms.open", {})
    _allow_echo(app)
    await app.call_tool("echo.ping", {})
    assert "echo" in app.instruments


@pytest.mark.asyncio
async def test_safety_violation_maps_to_error_result(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    _allow_echo(app)
    result = await app.call_tool("echo.boom_safety", {})
    assert result["error"]["type"] == "SafetyViolation"
    assert "over-voltage" in result["error"]["message"]


@pytest.mark.asyncio
async def test_instrument_not_configured_maps_to_error_result(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    _allow_echo(app)
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
    _allow_echo(app)
    await app.call_tool("echo.ping", {})  # instantiates echo
    echo = app.instruments["echo"]
    released = {"called": False}
    echo.release = lambda: released.update(called=True)  # type: ignore[method-assign]
    await app.call_tool("waveforms.close", {})
    assert released["called"] is True
    assert app.instruments == {}


@pytest.mark.asyncio
async def test_safety_log_writes_to_workspace_dir_after_open(tmp_path) -> None:
    """After waveforms.open(workspace_dir=...), gate_output must write the safety log
    to the user-specified dir, not fall through to the module logger.

    Regression test for the _workspace_raw coordination bug between Tasks 2 and 3.
    """
    app = build_app(backend_name="fake", workspace="")  # construct with no workspace
    await app.call_tool("waveforms.open", {"workspace_dir": str(tmp_path)})
    # Drive a successful gate_output via DwfDevice directly (Supply lands in Task 8).
    app.device.gate_output("supply_enable", channel="pos", voltage=3.0)
    log_path = tmp_path / "dwf-safety.log"
    files = list(tmp_path.iterdir())
    assert log_path.exists(), f"expected safety log at {log_path}, got files: {files}"
    content = log_path.read_text()
    assert "supply_enable" in content


@pytest.mark.asyncio
async def test_scope_configure_capture_close_flow(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.backend.set_scope_canned_data(  # type: ignore[attr-defined]
        {1: np.linspace(-1, 1, 512, dtype=np.float64)}
    )
    await app.call_tool("waveforms.open", {})
    cfg = await app.call_tool("scope.configure", {
        "channels": [1], "range_v": 5.0, "sample_rate_hz": 1_000_000, "buffer_size": 512,
    })
    assert cfg == {"configured": True}
    cap = await app.call_tool("scope.capture", {})
    assert "path" in cap
    assert "ch1" in cap["summary"]
    await app.call_tool("waveforms.close", {})


@pytest.mark.asyncio
async def test_scope_capture_before_configure_returns_error(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("scope.capture", {})
    assert result["error"]["type"] == "InstrumentNotConfigured"


@pytest.mark.asyncio
async def test_supply_set_enable_read_disable_flow(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {"supply_max_voltage_pos": 3.3})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0, "current_limit": 0.4})
    enable_result = await app.call_tool("supply.enable", {"channel": "vpos"})
    assert enable_result == {"enabled": True, "channel": "vpos"}
    read_result = await app.call_tool("supply.read", {"channel": "vpos"})
    assert read_result["enabled"] is True
    assert read_result["requested"]["voltage"] == 3.0
    await app.call_tool("supply.disable", {"channel": "vpos"})
    await app.call_tool("waveforms.close", {})


@pytest.mark.asyncio
async def test_supply_enable_above_cap_returns_safety_error(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {"supply_max_voltage_pos": 3.3})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 5.0})
    result = await app.call_tool("supply.enable", {"channel": "vpos"})
    assert result["error"]["type"] == "SafetyViolation"
    assert "5.0" in result["error"]["message"]


@pytest.mark.asyncio
async def test_i2c_configure_scan_flow(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.backend.set_i2c_acks({0x50: True, 0x68: True})  # type: ignore[attr-defined]
    await app.call_tool("waveforms.open", {})
    await app.call_tool("i2c.configure", {
        "sda_pin": "dio0", "scl_pin": "dio1", "clock_hz": 100_000,
    })
    scan = await app.call_tool("i2c.scan", {})
    assert scan["found"] == [0x50, 0x68]
    await app.call_tool("waveforms.close", {})


@pytest.mark.asyncio
async def test_i2c_write_before_configure_returns_error(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("i2c.write", {"address": 0x50, "data": [0x00]})
    assert result["error"]["type"] == "InstrumentNotConfigured"
