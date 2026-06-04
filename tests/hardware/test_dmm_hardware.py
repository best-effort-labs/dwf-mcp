"""DMM hardware smoke test. Requires W1→Scope1+ loopback and AD3 connected."""
from __future__ import annotations

import asyncio
import pytest

from dwf_mcp.server import build_app


@pytest.mark.hardware
def test_dmm_measures_awg_dc_voltage() -> None:
    app = build_app(backend_name="pydwf")

    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        await app.call_tool("awg.configure", {
            "channel": 1, "function": "DC",
            "frequency_hz": 1000.0, "amplitude_v": 2.0,
            "offset_v": 0.0, "phase_deg": 0.0, "symmetry": 50.0,
        })
        await app.call_tool("awg.start", {"channel": 1})
        import time; time.sleep(0.05)
        result = await app.call_tool("dmm.measure", {"channel": 1, "range_v": 5.0})
        assert "mean_v" in result
        assert abs(result["mean_v"] - 2.0) < 0.1, f"expected ~2.0V, got {result['mean_v']}"
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("waveforms.close", {})

    asyncio.run(run())
