"""Hardware smoke tests for scope.record_start/stop.

Wiring required:
    W1 (AWG ch1 output) → scope ch1 (1+ / 1-)
    W2 (AWG ch2 output, optional) → scope ch2

Run with:
    pytest tests/hardware/test_scope_record_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("scope_record")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    result = asyncio.get_event_loop().run_until_complete(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.get_event_loop().run_until_complete(app.call_tool("waveforms.close", {}))


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={"ch1": ("W1", "CH1_POS")})
async def test_scope_record_dc_signal(app, tmp_path: Path) -> None:
    """Record a DC signal from W1 and verify mean voltage is approximately correct."""
    # Set W1 to DC at 2.0V
    await app.call_tool("awg.configure", {
        "channel": 1,
        "function": "DC",
        "frequency_hz": 1000.0,
        "amplitude_v": 0.0,
        "offset_v": 2.0,
        "phase_deg": 0.0,
    })
    await app.call_tool("awg.start", {"channel": 1})

    out_path = tmp_path / "scope_record_dc.npz"
    result = await app.call_tool("scope.record_start", {
        "channels": [1],
        "range_v": 5.0,
        "sample_rate_hz": 100_000.0,
        "duration_s": 0.2,
        "output_path": str(out_path),
    })
    record_id = result["record_id"]

    # Wait for completion
    for _ in range(50):
        status = await app.call_tool("scope.record_status", {"record_id": record_id})
        if status["done"]:
            break
        await asyncio.sleep(0.05)

    stop = await app.call_tool("scope.record_stop", {"record_id": record_id})
    await app.call_tool("awg.stop", {"channel": 1})

    assert stop["artifact_error"] is None, f"artifact_error: {stop['artifact_error']}"
    assert stop["artifact_path"] is not None
    assert Path(stop["artifact_path"]).exists()
    assert stop["lost_samples"] == 0, f"lost {stop['lost_samples']} samples"

    data = np.load(stop["artifact_path"])
    assert "ch1" in data
    mean_v = float(data["ch1"].mean())
    assert abs(mean_v - 2.0) < 0.3, f"expected ~2.0V DC, got {mean_v:.3f}V"


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={"ch1": ("W1", "CH1_POS"), "ch2": ("W2", "CH2_POS")})
async def test_scope_record_two_channels(app, tmp_path: Path) -> None:
    """Record both channels simultaneously."""
    # W1 = 1.5V DC, W2 = -1.0V DC (if wired)
    await app.call_tool("awg.configure", {
        "channel": 1, "function": "DC", "frequency_hz": 1000.0,
        "amplitude_v": 0.0, "offset_v": 1.5, "phase_deg": 0.0,
    })
    await app.call_tool("awg.start", {"channel": 1})
    await app.call_tool("awg.configure", {
        "channel": 2, "function": "DC", "frequency_hz": 1000.0,
        "amplitude_v": 0.0, "offset_v": -1.0, "phase_deg": 0.0,
    })
    await app.call_tool("awg.start", {"channel": 2})

    result = await app.call_tool("scope.record_start", {
        "channels": [1, 2],
        "range_v": 5.0,
        "sample_rate_hz": 50_000.0,
        "duration_s": 0.2,
    })
    record_id = result["record_id"]

    for _ in range(50):
        status = await app.call_tool("scope.record_status", {"record_id": record_id})
        if status["done"]:
            break
        await asyncio.sleep(0.05)

    stop = await app.call_tool("scope.record_stop", {"record_id": record_id})
    await app.call_tool("awg.stop", {"channel": 1})
    await app.call_tool("awg.stop", {"channel": 2})

    assert stop["artifact_error"] is None
    data = np.load(stop["artifact_path"])
    assert "ch1" in data and "ch2" in data
    assert abs(float(data["ch1"].mean()) - 1.5) < 0.3
    assert abs(float(data["ch2"].mean()) - (-1.0)) < 0.3
