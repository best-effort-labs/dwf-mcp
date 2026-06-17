"""DMM hardware smoke test. Requires W1→CH1_POS loopback and AD3 connected."""
from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.hardware
@pytest.mark.requires(instruments={"dmm", "awg"})
@pytest.mark.jumperless(connections={
    # AD3 scope inputs are differential — CH1_NEG must be tied to AD3_GND
    # and AD3_GND must share a reference with the Jumperless signal ground.
    "gnd_bridge": ("AD3_GND", "GND"),
    "ch1_neg": ("CH1_NEG", "AD3_GND"),
    "awg_to_scope": ("W1", "CH1_POS"),
})
def test_dmm_measures_awg_dc_voltage(app) -> None:
    async def run() -> None:
        await app.call_tool("awg.configure", {
            "channel": 1, "function": "DC",
            "frequency_hz": 1000.0, "amplitude_v": 0.0,
            "offset_v": 2.0, "phase_deg": 0.0, "symmetry": 50.0,
        })
        await app.call_tool("awg.start", {"channel": 1})
        time.sleep(0.05)
        result = await app.call_tool("dmm.measure", {"channel": 1, "range_v": 5.0})
        assert "mean_v" in result
        assert abs(result["mean_v"] - 2.0) < 0.1, f"expected ~2.0V, got {result['mean_v']}"
        await app.call_tool("awg.stop", {"channel": 1})

    asyncio.run(run())
