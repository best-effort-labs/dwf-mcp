"""Hardware smoke tests for logic.record_start/stop.

Wiring required:
    DIO0 (Pattern clock output) → DIO1 (Logic record input) via Jumperless loopback.

Run with:
    pytest tests/hardware/test_logic_record_hardware.py -v -m hardware
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
        workspace=str(tmp_path_factory.mktemp("logic_record")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app):
    result = asyncio.run(app.call_tool("waveforms.open", {}))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.run(app.call_tool("waveforms.close", {}))


@pytest.mark.asyncio
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
async def test_logic_record_clock_signal(app, tmp_path: Path) -> None:
    """Record a 10 kHz clock from Pattern on DIO0, captured via DIO1, and verify transitions."""
    await app.call_tool("pattern.configure", {
        "pin": "dio0",
        "function": "Clock",
        "frequency_hz": 10_000.0,
        "duty": 0.5,
        "idle_state": "low",
    })
    await app.call_tool("pattern.start", {"pin": "dio0"})

    out_path = tmp_path / "logic_record_clock.npz"
    result = await app.call_tool("logic.record_start", {
        "pins": ["dio1"],
        "sample_rate_hz": 1_000_000.0,
        "duration_s": 0.3,
        "output_path": str(out_path),
    })
    record_id = result["record_id"]

    # Wait for completion (0.3s + overhead; poll at 50ms intervals)
    for _ in range(40):
        status = await app.call_tool("logic.record_status", {"record_id": record_id})
        if status["done"]:
            break
        await asyncio.sleep(0.05)

    stop = await app.call_tool("logic.record_stop", {"record_id": record_id})
    await app.call_tool("pattern.stop", {"pin": "dio0"})

    assert stop["artifact_error"] is None, f"artifact_error: {stop['artifact_error']}"
    assert stop["artifact_path"] is not None
    assert Path(stop["artifact_path"]).exists()
    assert stop["lost_samples"] == 0, f"lost {stop['lost_samples']} samples"

    data = np.load(stop["artifact_path"])
    assert "dio1" in data, f"expected 'dio1' key in npz, got: {list(data.keys())}"
    dio1 = data["dio1"]

    # At 1 MHz sample rate and 10 kHz clock, expect ~100 samples per period and ~3000 cycles.
    # Both 0 and 1 must be present, and transitions should be abundant.
    assert 0 in dio1 and 1 in dio1, "expected clock transitions on DIO1 — got flat signal"
    transitions = int(np.sum(np.diff(dio1.astype(np.int8)) != 0))
    assert transitions >= 100, f"expected ≥100 transitions, got {transitions}"
