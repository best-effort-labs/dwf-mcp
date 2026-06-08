#!/usr/bin/env python3
"""Quick diagnostic: check actual AnalogIn range and read CH2 directly.

Usage: pytest tests/hardware/diag_ai_range.py -v --no-header -m hardware -s
"""
from __future__ import annotations
import asyncio
import time
import pytest


@pytest.mark.hardware
def test_ai_range_check(app) -> None:
    """Report actual scope range and measure CH2 at several W2 voltages."""
    from pydwf import DwfAcquisitionMode, DwfAnalogCoupling, DwfTriggerSource, DwfState
    backend = app.device.backend
    ai = backend._device.analogIn

    print("\n=== AnalogIn range check ===")

    ai.reset()
    for ch in (0, 1):
        ai.channelEnableSet(ch, True)
        ai.channelRangeSet(ch, 5.0)
        ai.channelOffsetSet(ch, 0.0)
        ai.channelCouplingSet(ch, DwfAnalogCoupling.DC)

    # Read back what range was actually set
    r0 = ai.channelRangeGet(0)
    r1 = ai.channelRangeGet(1)
    print(f"  CH1 range set: {r0}V")
    print(f"  CH2 range set: {r1}V")

    ai.frequencySet(100_000.0)
    ai.bufferSizeSet(512)
    ai.acquisitionModeSet(DwfAcquisitionMode.Single)
    ai.triggerSourceSet(DwfTriggerSource.None_)

    async def awg_set(v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": 2, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": 2})

    async def awg_stop() -> None:
        await app.call_tool("awg.stop", {"channel": 2})

    # Warmup: one dummy AWG cycle so the first real measurement isn't stale
    asyncio.run(awg_set(0.0))
    time.sleep(0.05)
    ai.configure(False, True)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if ai.status(True) == DwfState.Done:
            break
        time.sleep(0.001)
    asyncio.run(awg_stop())

    print("\n  W2 voltage scan (wire W2->CH2_POS, GND->CH2_NEG):")
    for v in [-1.0, -0.5, 0.0, +0.5, +1.0, +1.5]:
        asyncio.run(awg_set(v))
        time.sleep(0.05)

        ai.configure(False, True)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if ai.status(True) == DwfState.Done:
                break
            time.sleep(0.001)

        s1 = ai.statusData(1, 512)
        mean = sum(s1) / len(s1)
        print(f"  W2={v:+.1f}V  CH2={mean:+.5f}V  ratio={mean/v:.3f}" if v != 0 else f"  W2={v:+.1f}V  CH2={mean:+.5f}V")
        asyncio.run(awg_stop())

    ai.reset()
