"""Hardware smoke test for logic.record_start/stop on the Digital Discovery (devid 4).

Wiring (DD left side connector): DIO24 (Pattern clock) -> DIO25 (Logic record input)
via Jumperless loopback, plus JL GND -> DD GND (mandatory crossbar reference).

Run with:
    DWF_TEST_SERIAL=210321AD4ECF pytest \
        tests/hardware/test_logic_record_dd_hardware.py -v -m hardware
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    from dwf_mcp.server import build_app
    return build_app(
        backend_name="pydwf",
        workspace=str(tmp_path_factory.mktemp("logic_record_dd")),
    )


@pytest.fixture(scope="module", autouse=True)
def open_device(app, request):
    # Honor DWF_TEST_SERIAL to target the wired DUT.
    args = {}
    serial = os.environ.get("DWF_TEST_SERIAL")
    if serial:
        args["device_serial"] = serial
    result = asyncio.run(app.call_tool("waveforms.open", args))
    assert "device" in result, f"Failed to open device: {result}"
    yield
    asyncio.run(app.call_tool("waveforms.close", {}))


@pytest.mark.asyncio
@pytest.mark.requires(pins={"dio24", "dio25"})
@pytest.mark.jumperless(connections={"loopback": ("DIO24", "DIO25"), "gnd": ("DD_GND", "GND")})
async def test_dd_logic_record_clock_signal(app, tmp_path: Path) -> None:
    """Record a 10 kHz clock from Pattern on DIO24, captured via DIO25, verify transitions."""
    await app.call_tool("pattern.configure", {
        "pin": "dio24", "function": "Clock", "frequency_hz": 10_000.0,
        "duty": 0.5, "idle_state": "low",
    })
    await app.call_tool("pattern.start", {"pin": "dio24"})

    out_path = tmp_path / "logic_record_dd_clock.npz"
    result = await app.call_tool("logic.record_start", {
        "pins": ["dio25"], "sample_rate_hz": 1_000_000.0,
        "duration_s": 0.3, "output_path": str(out_path),
    })
    record_id = result["record_id"]

    for _ in range(40):
        status = await app.call_tool("logic.record_status", {"record_id": record_id})
        if status["done"]:
            break
        await asyncio.sleep(0.05)

    stop = await app.call_tool("logic.record_stop", {"record_id": record_id})
    await app.call_tool("pattern.stop", {"pin": "dio24"})

    assert stop["artifact_error"] is None, f"artifact_error: {stop['artifact_error']}"
    assert stop["artifact_path"] is not None
    assert Path(stop["artifact_path"]).exists()
    assert stop["lost_samples"] == 0, f"lost {stop['lost_samples']} samples"

    data = np.load(stop["artifact_path"])
    assert "dio25" in data, f"expected 'dio25' key in npz, got: {list(data.keys())}"
    dio25 = data["dio25"]
    assert 0 in dio25 and 1 in dio25, "expected clock transitions on DIO25 — got flat signal"
    transitions = int(np.sum(np.diff(dio25.astype(np.int8)) != 0))
    assert transitions >= 100, f"expected >=100 transitions, got {transitions}"
