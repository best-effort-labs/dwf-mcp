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
async def test_instrument_tool_before_open_returns_clean_error(tmp_path) -> None:
    """Calling an instrument tool before waveforms.open should return a clean
    DwfDeviceLost error dict, not crash inside the instrument constructor."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    # Device deliberately NOT opened.
    result = await app.call_tool("supply.set", {"channel": "vplus", "voltage": 3.3})
    assert "error" in result, f"expected error dict, got {result}"
    assert result["error"]["type"] == "DwfDeviceLost"


@pytest.mark.asyncio
async def test_unknown_tool_raises_valueerror(tmp_path) -> None:
    """call_tool with an unregistered tool name raises ValueError immediately,
    before any device interaction. Caller can distinguish from runtime errors."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    with pytest.raises(ValueError, match="unknown tool"):
        await app.call_tool("nonexistent.thing", {})


@pytest.mark.asyncio
async def test_waveforms_open_returns_device_info(tmp_path) -> None:
    """waveforms.open returns a device info dict with serial/model/firmware."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    result = await app.call_tool("waveforms.open", {})
    assert "device" in result
    assert "serial" in result["device"]
    assert "model" in result["device"]
    assert "firmware" in result["device"]
    assert result["workspace"] == str(tmp_path)


@pytest.mark.asyncio
async def test_waveforms_status_reflects_open_state(tmp_path) -> None:
    """waveforms.status returns open=False before waveforms.open, open=True after."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    status_before = await app.call_tool("waveforms.status", {})
    assert status_before["open"] is False
    await app.call_tool("waveforms.open", {})
    status_after = await app.call_tool("waveforms.status", {})
    assert status_after["open"] is True


@pytest.mark.asyncio
async def test_waveforms_list_pins_requires_open(tmp_path) -> None:
    """waveforms.list_pins returns an error dict when device isn't open."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    result = await app.call_tool("waveforms.list_pins", {})
    assert "error" in result
    assert result["error"]["type"] == "DwfDeviceLost"


@pytest.mark.asyncio
async def test_waveforms_close_releases_all_instruments(tmp_path) -> None:
    """waveforms.close clears the lazy-instantiated instrument cache so a fresh
    open starts with a clean slate."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    # Touch dio to lazy-construct it
    await app.call_tool("dio.set_direction", {"pin": "dio0", "direction": "out"})
    assert "dio" in app.instruments
    await app.call_tool("waveforms.close", {})
    assert app.instruments == {}


@pytest.mark.asyncio
async def test_tick_idle_closes_device_after_timeout(tmp_path) -> None:
    """If idle_timeout_s elapses between tool calls, the device auto-closes
    on the next call_tool invocation (tick_idle runs first)."""
    import time
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path), idle_timeout_s=0.001)
    await app.call_tool("waveforms.open", {})
    assert app.device.is_open
    # Backdate last activity to force the idle timer to expire on next tick.
    app.device._last_activity = time.monotonic() - 1.0
    # call_tool runs tick_idle first; the device should be closed before the
    # status response is built (status reports open=False).
    status = await app.call_tool("waveforms.status", {})
    assert status["open"] is False


@pytest.mark.asyncio
async def test_get_or_create_instrument_caches_instance(tmp_path) -> None:
    """Lazy instrument construction returns the same instance on repeated calls."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    # First call constructs; second call should return the cached instance.
    await app.call_tool("dio.set_direction", {"pin": "dio0", "direction": "out"})
    first = app.instruments["dio"]
    await app.call_tool("dio.set_direction", {"pin": "dio1", "direction": "out"})
    second = app.instruments["dio"]
    assert first is second


@pytest.mark.asyncio
async def test_call_tool_wraps_pin_allocation_error(tmp_path) -> None:
    """PinAllocationError raised mid-tool is converted to {"error": {...}}
    rather than propagating to the caller."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    # First i2c.configure claims dio0/dio1. Second logic.configure on
    # overlapping pins triggers PinAllocationError.
    await app.call_tool("i2c.configure", {
        "sda_pin": "dio0", "scl_pin": "dio1", "clock_hz": 100_000,
    })
    result = await app.call_tool("logic.configure", {
        "pins": ["dio0"], "sample_rate_hz": 1e6, "buffer_size": 1024,
    })
    assert "error" in result
    assert result["error"]["type"] == "PinAllocationError"


@pytest.mark.asyncio
async def test_call_tool_wraps_safety_violation(tmp_path) -> None:
    """SafetyViolation (e.g. supply voltage over policy max) is returned as
    an error dict, not raised."""
    from dwf_mcp.server import build_app
    app = build_app(
        backend_name="fake", workspace=str(tmp_path),
    )
    await app.call_tool("waveforms.open", {"supply_max_voltage_pos": 3.3})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 5.0})
    result = await app.call_tool("supply.enable", {"channel": "vpos"})
    assert "error" in result
    assert result["error"]["type"] == "SafetyViolation"


@pytest.mark.asyncio
async def test_call_tool_wraps_instrument_not_configured(tmp_path) -> None:
    """InstrumentNotConfigured raised by an instrument method (e.g. i2c.write
    before i2c.configure) is converted to an error dict, not propagated."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    # Skip i2c.configure; go straight to i2c.write.
    result = await app.call_tool("i2c.write", {"address": 0x42, "data": [0x00]})
    assert "error" in result
    assert result["error"]["type"] == "InstrumentNotConfigured"


class _RaisingInstrument(Instrument):
    """Test-only instrument whose only tool raises a non-mapped exception."""
    name = "raising_test"
    tools: ClassVar[dict[str, Any]] = {
        "boom": ("boom", {"type": "object", "properties": {}}),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        pass

    def boom(self) -> dict[str, Any]:
        raise ValueError("not in _ERROR_TYPES")

    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_call_tool_propagates_unmapped_exception(tmp_path) -> None:
    """Exceptions not in _ERROR_TYPES (e.g. ValueError from buggy instrument
    code) propagate to the caller rather than being silently swallowed."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})  # instrument tools require an open device
    app.registry.register(_RaisingInstrument)
    app._tools["raising_test.boom"] = app._make_instrument_handler("raising_test", "boom")
    app.instruments["raising_test"] = _RaisingInstrument(device=app.device, artifacts=app.artifacts)
    with pytest.raises(ValueError, match="not in _ERROR_TYPES"):
        await app.call_tool("raising_test.boom", {})


@pytest.mark.asyncio
async def test_waveforms_list_pins_returns_pin_inventory(tmp_path) -> None:
    """After waveforms.open, list_pins returns the full pin inventory with
    nothing claimed yet."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("waveforms.list_pins", {})
    assert "error" not in result
    assert "all_pins" in result
    assert "claimed" in result
    # No instruments have claimed yet, so claimed list is empty.
    assert result["claimed"] == {}
    # Inventory contains at least the AD3 DIO pins.
    assert any(p.startswith("dio") for p in result["all_pins"])


@pytest.mark.asyncio
async def test_handler_awaits_coroutine(tmp_path):
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})  # instrument tools require an open device
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


@pytest.mark.asyncio
async def test_call_tool_ticks_idle_on_live_instruments(tmp_path) -> None:
    """Every tool call should give live instruments a chance to reap idle state
    (e.g. orphan sniff sessions whose owner never called *_stop), regardless of
    which tool was actually invoked."""
    from dwf_mcp.server import build_app

    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})

    ticks = {"n": 0}

    class _SpyInstrument(Instrument):
        name = "spy"
        tools: ClassVar[dict[str, Any]] = {}

        def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
            pass

        def release(self) -> None:
            pass

        def tick_idle(self) -> None:
            ticks["n"] += 1

    app.instruments["spy"] = _SpyInstrument(app.device, app.artifacts)

    await app.call_tool("waveforms.status", {})  # unrelated tool
    assert ticks["n"] >= 1, "call_tool did not tick idle on live instruments"


@pytest.mark.asyncio
async def test_instrument_tool_after_unplug_returns_clean_error_and_resets(tmp_path) -> None:
    """A device unplugged between tool calls must be detected before dispatch:
    the instrument tool returns a clean DwfDeviceLost, and stale instrument +
    allocator state is cleared so a re-open starts fresh."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0})
    assert "supply" in app.instruments
    assert app.device.allocator.claimed_pins() != {}

    app.device.backend.simulate_unplug()  # type: ignore[attr-defined]

    result = await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0})
    assert result["error"]["type"] == "DwfDeviceLost"
    assert app.instruments == {}
    assert app.device.allocator.claimed_pins() == {}


@pytest.mark.asyncio
async def test_device_lost_mid_call_surfaces_clean_error(tmp_path, monkeypatch) -> None:
    """If the device vanishes during a handler (raw backend error + device now
    gone), call_tool surfaces a clean DwfDeviceLost and resets — rather than
    leaking the raw exception."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0})

    fake = app.device.backend

    def boom(*a, **k):
        fake._open_info = None  # device disappears mid-call  # noqa: SLF001
        raise RuntimeError("usb communication failed")

    monkeypatch.setattr(fake, "supply_node_set", boom)
    result = await app.call_tool("supply.set", {"channel": "vpos", "voltage": 5.0})
    assert result["error"]["type"] == "DwfDeviceLost"
    assert app.instruments == {}


@pytest.mark.asyncio
async def test_genuine_error_with_device_present_is_not_swallowed(tmp_path, monkeypatch) -> None:
    """A handler error while the device is still present is a real bug, not a
    disconnect — it must propagate, not be masked as DwfDeviceLost."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0})

    fake = app.device.backend

    def boom(*a, **k):
        raise RuntimeError("genuine backend bug")  # device stays present

    monkeypatch.setattr(fake, "supply_node_set", boom)
    with pytest.raises(RuntimeError, match="genuine backend bug"):
        await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0})


@pytest.mark.asyncio
async def test_idle_close_clears_instruments_immediately(tmp_path, monkeypatch) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path), idle_timeout_s=0.0)
    await app.call_tool("waveforms.open", {})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0})
    assert "supply" in app.instruments
    app.device.tick_idle()  # idle_timeout_s=0 -> closes -> on_close clears instruments
    assert not app.device.is_open
    assert app.instruments == {}


@pytest.mark.asyncio
async def test_open_different_serial_while_open_errors(tmp_path) -> None:
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("waveforms.open", {"device_serial": "OTHER-SERIAL"})
    assert "error" in result
    assert "already open" in result["error"]["message"].lower()


@pytest.mark.asyncio
async def test_unsupported_instrument_returns_error(tmp_path, monkeypatch) -> None:
    import dataclasses
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    # DeviceProfile is frozen; replace the whole profile (device.profile is mutable).
    app.device.profile = dataclasses.replace(
        app.device.profile, supported_instruments=frozenset({"scope"})
    )
    result = await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0})
    assert result["error"]["type"] == "InstrumentNotConfigured"
    assert "not available on this device" in result["error"]["message"]


@pytest.mark.asyncio
async def test_open_passes_device_config_strategy_to_backend(tmp_path) -> None:
    """waveforms.open(device_config=...) plumbs the strategy down to the backend,
    and the tool schema advertises the available strategies to the client."""
    from dwf_mcp.server import build_app
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {"device_config": "max_digital_in"})
    assert app.device.backend.last_device_config == "max_digital_in"
    # Schema advertises the enum so an LLM can discover/choose it.
    enum = app._tool_schemas["waveforms.open"]["properties"]["device_config"]["enum"]
    assert "max_digital_in" in enum and "default" in enum
