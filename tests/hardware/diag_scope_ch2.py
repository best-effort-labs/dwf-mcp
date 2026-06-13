#!/usr/bin/env python3
"""CH2 signal path diagnostic.

Part A: Direct jumper wires W2->CH2_POS AND GND->CH2_NEG (no Jumperless).
         Confirms the AD3 hardware path works when wired correctly.
Part B: Same test via Jumperless routing.
         Confirms whether cross-half (W2 row41 -> CH2_POS row14) works.
Part C: All three connections (W1->CH1_POS + W2->CH2_POS + CH2_NEG->GND).
         Confirms whether adding W1->CH1_POS conflicts with W2->CH2_POS.
Part D: Alternative routing — W2 via intermediate row to CH2_POS.
         Workaround attempt if rows 14/15 share CH446Q resources.

Usage: pytest tests/hardware/diag_scope_ch2.py::test_direct_jumper -v --no-header -m hardware -s
       pytest tests/hardware/diag_scope_ch2.py::test_jumperless_crosshalf -v --no-header -m hardware -s
       pytest tests/hardware/diag_scope_ch2.py::test_jumperless_all_three -v --no-header -m hardware -s
       pytest tests/hardware/diag_scope_ch2.py::test_jumperless_alt_routing -v --no-header -m hardware -s
"""
from __future__ import annotations
import asyncio
import time
import pytest


NBUF = 512

def _setup_ai_single(ai) -> None:
    from pydwf import DwfAcquisitionMode, DwfAnalogCoupling, DwfTriggerSource
    ai.reset()
    for ch in (0, 1):
        ai.channelEnableSet(ch, True)
        ai.channelRangeSet(ch, 5.0)
        ai.channelOffsetSet(ch, 0.0)
        ai.channelCouplingSet(ch, DwfAnalogCoupling.DC)
    ai.frequencySet(100_000.0)
    ai.bufferSizeSet(NBUF)
    ai.acquisitionModeSet(DwfAcquisitionMode.Single)
    ai.triggerSourceSet(DwfTriggerSource.None_)  # auto-trigger immediately


def _read_both(ai) -> tuple[float, float]:
    """Single acquisition, read both channels."""
    from pydwf import DwfState
    ai.configure(False, True)
    deadline = time.monotonic() + 0.5
    done = False
    while time.monotonic() < deadline:
        if ai.status(True) == DwfState.Done:
            done = True
            break
        time.sleep(0.001)
    if not done:
        raise RuntimeError("scope single-shot did not complete within 0.5s")
    s1 = ai.statusData(1, NBUF)  # read ch1 only, skip ch0
    return 0.0, float(sum(s1) / len(s1))


@pytest.mark.hardware
def test_direct_jumper(app) -> None:
    """Part A: Manual direct wires — no Jumperless.

    Before running, physically connect:
      - W2 (AWG ch2 output) -> CH2_POS (2+ / scope ch2 positive input)
      - GND -> CH2_NEG (2- / scope ch2 negative input)
    """
    backend = app.device.backend
    ai = backend._device.analogIn

    print("\n=== Part A: Direct jumper wires ===")
    print("Requires: W2->CH2_POS AND GND->CH2_NEG wired directly on AD3.")

    async def awg_set(v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": 2, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": 2})

    async def awg_stop() -> None:
        await app.call_tool("awg.stop", {"channel": 2})

    print("  Using record mode (same as test_scope_record_two_channels):")
    import numpy as np

    for v in [-1.0, 0.0, +1.0, +1.5]:
        asyncio.run(awg_set(v))
        time.sleep(0.05)

        # Use the MCP record tools directly
        async def do_record(target_v):
            r = await app.call_tool("scope.record_start", {
                "channels": [2],
                "range_v": 5.0,
                "sample_rate_hz": 50_000.0,
                "duration_s": 0.1,
            })
            rid = r["record_id"]
            for _ in range(30):
                st = await app.call_tool("scope.record_status", {"record_id": rid})
                if st["done"]:
                    break
                await asyncio.sleep(0.02)
            stop = await app.call_tool("scope.record_stop", {"record_id": rid})
            data = np.load(stop["artifact_path"])
            ch2_mean = float(data["ch2"].mean()) if "ch2" in data else None
            return ch2_mean

        reading = asyncio.run(do_record(v))
        ok = reading is not None and abs(reading - v) < 0.3
        print(f"  W2={v:+.1f}V  CH2(record)={reading:+.5f}V  {'✓' if ok else '✗'}")
        asyncio.run(awg_stop())

    print("\nAll ✓ = AD3 hardware OK with record mode.")


@pytest.mark.hardware
@pytest.mark.jumperless(connections={
    "ch2_pos": ("W2", "CH2_POS"),
    "ch2_neg": ("CH2_NEG", "GND"),
})
def test_jumperless_crosshalf(app, jumperless) -> None:
    """Part B: Same test via Jumperless routing.

    Connections routed by Jumperless:
      - W2 (row 41, bottom half) -> CH2_POS (row 14, top half)   [cross-half]
      - CH2_NEG (row 44, bottom half) -> GND node                [same-half]
    """
    from tests.hardware import pinout
    backend = app.device.backend
    ai = backend._device.analogIn

    print("\n=== Part B: Jumperless cross-half routing ===")
    print(f"  W2=row{pinout.row('W2')}, CH2_POS=row{pinout.row('CH2_POS')}, CH2_NEG=row{pinout.row('CH2_NEG')}")

    async def awg_set(v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": 2, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": 2})

    async def awg_stop() -> None:
        await app.call_tool("awg.stop", {"channel": 2})

    _setup_ai_single(ai)

    for v in [-1.0, -0.5, 0.0, +0.5, +1.0]:
        asyncio.run(awg_set(v))
        time.sleep(0.1)
        _, r1 = _read_both(ai)
        ok = abs(r1 - v) < 0.3
        print(f"  W2={v:+.1f}V  CH2={r1:+.4f}V  {'✓' if ok else '✗ FAIL'}")
        asyncio.run(awg_stop())

    ai.reset()

    # Also check W2 output via Jumperless ADC as ground truth
    if jumperless is not None:
        print("\n  W2 output via Jumperless ADC0:")
        w2_row = pinout.row("W2")
        asyncio.run(awg_set(-1.0))
        time.sleep(0.05)
        jumperless.connect(w2_row, "ADC0")
        time.sleep(0.05)
        v_adc = jumperless.adc_get(0)
        print(f"  W2 measured by ADC0: {v_adc:+.4f}V (expect ~-1.0V if W2 output is working)")
        jumperless.disconnect(w2_row, "ADC0")
        asyncio.run(awg_stop())


@pytest.mark.hardware
def test_jumperless_all_three(app, jumperless) -> None:
    """Part C: All three connections at once — confirms whether W1->CH1_POS conflicts.

    Routes:
      - W1  (row 11, top) -> CH1_POS (row 15, top)   [same-half]
      - W2  (row 41, bot) -> CH2_POS (row 14, top)   [cross-half]
      - CH2_NEG (row 44, bot) -> GND                 [same-half]
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    from tests.hardware import pinout
    backend = app.device.backend
    ai = backend._device.analogIn

    w1_row = pinout.row("W1")
    ch1_pos_row = pinout.row("CH1_POS")
    w2_row = pinout.row("W2")
    ch2_pos_row = pinout.row("CH2_POS")
    ch2_neg_row = pinout.row("CH2_NEG")

    print("\n=== Part C: All three connections simultaneously ===")
    print(f"  W1=row{w1_row}->CH1_POS=row{ch1_pos_row}")
    print(f"  W2=row{w2_row}->CH2_POS=row{ch2_pos_row}")
    print(f"  CH2_NEG=row{ch2_neg_row}->GND")

    async def awg_set(ch: int, v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": ch, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": ch})

    async def awg_stop_all() -> None:
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("awg.stop", {"channel": 2})

    jumperless.nodes_clear()
    jumperless.connect(w2_row, ch2_pos_row)
    jumperless.connect(ch2_neg_row, "GND")
    jumperless.connect(w1_row, ch1_pos_row)
    time.sleep(0.1)

    _setup_ai_single(ai)

    # Warmup
    asyncio.run(awg_set(1, 1.5))
    asyncio.run(awg_set(2, 0.0))
    time.sleep(0.05)
    _read_both(ai)

    print("\n  CH1=1.5V fixed, scanning W2 voltage:")
    asyncio.run(awg_set(1, 1.5))
    for v in [-1.0, -0.5, 0.0, +0.5, +1.0]:
        asyncio.run(awg_set(2, v))
        time.sleep(0.1)
        _, ch2 = _read_both(ai)
        ok = abs(ch2 - v) < 0.3
        print(f"  W2={v:+.1f}V  CH2={ch2:+.4f}V  {'✓' if ok else '✗ FAIL (conflict?)'}")

    asyncio.run(awg_stop_all())

    # Check Jumperless ADC to verify W2 is actually outputting
    if True:
        print("\n  Verifying W2 output via Jumperless ADC0 (W2->ADC0 added):")
        asyncio.run(awg_set(2, -1.0))
        time.sleep(0.05)
        jumperless.connect(w2_row, "ADC0")
        time.sleep(0.05)
        v_adc = jumperless.adc_get(0)
        jumperless.disconnect(w2_row, "ADC0")
        print(f"  W2 via ADC0: {v_adc:+.4f}V (expect ~-1.0V)")
        asyncio.run(awg_stop_all())

    jumperless.nodes_clear()
    ai.reset()


@pytest.mark.hardware
def test_jumperless_w1_via_bot(app, jumperless) -> None:
    """Part G: Route W1 through a bottom-half intermediate row to CH1_POS.

    Forces W1->CH1_POS to take a cross-half path (using different chips than
    the direct same-half path chip1->chip2), potentially avoiding the conflict
    with W2->CH2_POS which uses chip5->chip1.

    W1 (row11, top) -> row50 (bottom, arbitrary) -> CH1_POS (row15, top)
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    from tests.hardware import pinout
    backend = app.device.backend
    ai = backend._device.analogIn

    w1_row = pinout.row("W1")
    ch1_pos_row = pinout.row("CH1_POS")
    w2_row = pinout.row("W2")
    ch2_pos_row = pinout.row("CH2_POS")
    ch2_neg_row = pinout.row("CH2_NEG")

    # Bottom-half intermediate rows to try for W1 hop
    # Avoid rows near W2(41), CH2_NEG(44) — try 35 and 50
    hop_rows = [35, 50, 55]

    async def awg_set(ch: int, v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": ch, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": ch})

    async def awg_stop_all() -> None:
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("awg.stop", {"channel": 2})

    print("\n=== Part G: W1 via bottom-half hop to CH1_POS ===")
    print(f"  W1=row{w1_row}, CH1_POS=row{ch1_pos_row}, W2=row{w2_row}, CH2_POS=row{ch2_pos_row}")

    for hop in hop_rows:
        print(f"\n  --- Hop row {hop} ---")
        jumperless.nodes_clear()
        asyncio.run(awg_set(1, 1.5))
        asyncio.run(awg_set(2, -1.0))

        # W2 path first
        jumperless.connect(w2_row, ch2_pos_row)
        jumperless.connect(ch2_neg_row, "GND")
        # W1 via bottom-half hop
        jumperless.connect(w1_row, hop)
        jumperless.connect(hop, ch1_pos_row)
        time.sleep(0.1)

        paths = jumperless.eval_json("get_all_paths()")
        w2_path = jumperless.eval_json(f"get_path_between({w2_row}, {ch2_pos_row})")
        w1_paths = [p for p in paths if p.get("node1") in (w1_row, hop, ch1_pos_row) or p.get("node2") in (w1_row, hop, ch1_pos_row)]
        print(f"  W2->CH2_POS:  {w2_path}")
        for p in w1_paths:
            print(f"  W1 hop path:  {p}")

        _setup_ai_single(ai)
        asyncio.run(awg_set(1, 1.5))
        asyncio.run(awg_set(2, 0.0))
        time.sleep(0.05)
        _read_both(ai)  # warmup

        asyncio.run(awg_set(1, 1.5))
        for v in [-1.0, 0.0, +1.0]:
            asyncio.run(awg_set(2, v))
            time.sleep(0.1)
            _, ch2 = _read_both(ai)
            ok = abs(ch2 - v) < 0.3
            print(f"  W2={v:+.1f}V  CH2={ch2:+.4f}V  {'✓' if ok else '✗ FAIL'}")

        asyncio.run(awg_stop_all())
        ai.reset()

    jumperless.nodes_clear()


@pytest.mark.hardware
def test_jumperless_alt_routing(app, jumperless) -> None:
    """Part D: Alternative routing — W2 via intermediate row to avoid CH446Q conflict.

    Instead of W2 (row41) direct to CH2_POS (row14), route via an intermediate
    row in the top half that is not adjacent to CH1_POS (row15):
      - W2 (row41) -> row12 (intermediate, top half)
      - row12 -> CH2_POS (row14)
      - CH2_NEG (row44) -> GND
      - W1 (row11) -> CH1_POS (row15)
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    from tests.hardware import pinout
    backend = app.device.backend
    ai = backend._device.analogIn

    w1_row = pinout.row("W1")
    ch1_pos_row = pinout.row("CH1_POS")
    w2_row = pinout.row("W2")
    ch2_pos_row = pinout.row("CH2_POS")
    ch2_neg_row = pinout.row("CH2_NEG")

    # Pick an intermediate row in the top half away from rows 14/15
    # Top rows: 1-15, CH2_POS=14, CH1_POS=15 — use row 10 as hop
    hop_row = 10

    print("\n=== Part D: Alternative routing via hop row ===")
    print(f"  W2=row{w2_row} -> row{hop_row} -> CH2_POS=row{ch2_pos_row}")
    print(f"  W1=row{w1_row} -> CH1_POS=row{ch1_pos_row}")
    print(f"  CH2_NEG=row{ch2_neg_row} -> GND")

    async def awg_set(ch: int, v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": ch, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": ch})

    async def awg_stop_all() -> None:
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("awg.stop", {"channel": 2})

    jumperless.nodes_clear()
    jumperless.connect(w2_row, hop_row)
    jumperless.connect(hop_row, ch2_pos_row)
    jumperless.connect(ch2_neg_row, "GND")
    jumperless.connect(w1_row, ch1_pos_row)
    time.sleep(0.1)

    _setup_ai_single(ai)

    # Warmup
    asyncio.run(awg_set(1, 1.5))
    asyncio.run(awg_set(2, 0.0))
    time.sleep(0.05)
    _read_both(ai)

    print("\n  CH1=1.5V fixed, scanning W2 voltage via hop:")
    asyncio.run(awg_set(1, 1.5))
    for v in [-1.0, -0.5, 0.0, +0.5, +1.0]:
        asyncio.run(awg_set(2, v))
        time.sleep(0.1)
        _, ch2 = _read_both(ai)
        ok = abs(ch2 - v) < 0.3
        print(f"  W2={v:+.1f}V  CH2={ch2:+.4f}V  {'✓' if ok else '✗ FAIL'}")

    asyncio.run(awg_stop_all())
    jumperless.nodes_clear()
    ai.reset()


@pytest.mark.hardware
def test_jumperless_w2_pull(app, jumperless) -> None:
    """Part F: Isolate why W2 row itself reads ~0V when W1->CH1_POS is added.

    1. W2->CH2_POS + CH2_NEG->GND only: measure W2 via ADC (expect -1.0V)
    2. Add W1->CH1_POS: re-measure W2 via ADC (does it drop to 0V?)
    3. Reverse order (W1->CH1_POS first): does W2 path still fail?
    4. W1=W2=+1.5V: if it's a short, voltages match, ADC should show +1.5V
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    from tests.hardware import pinout

    w1_row = pinout.row("W1")
    ch1_pos_row = pinout.row("CH1_POS")
    w2_row = pinout.row("W2")
    ch2_pos_row = pinout.row("CH2_POS")
    ch2_neg_row = pinout.row("CH2_NEG")

    async def awg_set(ch: int, v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": ch, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": ch})

    async def awg_stop_all() -> None:
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("awg.stop", {"channel": 2})

    def measure_node_adc(row_num: int) -> float:
        jumperless.connect(row_num, "ADC0")
        time.sleep(0.05)
        v = jumperless.adc_get(0)
        jumperless.disconnect(row_num, "ADC0")
        return v

    print("\n=== Part F: W2 row voltage before/after W1->CH1_POS ===")
    print(f"  W1=row{w1_row}, CH1_POS=row{ch1_pos_row}, W2=row{w2_row}, CH2_POS=row{ch2_pos_row}")

    # Step 1: W2->CH2_POS + CH2_NEG->GND only
    jumperless.nodes_clear()
    asyncio.run(awg_set(2, -1.0))
    jumperless.connect(w2_row, ch2_pos_row)
    jumperless.connect(ch2_neg_row, "GND")
    time.sleep(0.1)
    v_before = measure_node_adc(w2_row)
    print(f"  Step1 (W2->CH2_POS + CH2_NEG->GND, no W1): W2 ADC={v_before:+.4f}V  (expect ~-1.0V)")

    # Step 2: add W1->CH1_POS, W1=+1.5V
    asyncio.run(awg_set(1, 1.5))
    jumperless.connect(w1_row, ch1_pos_row)
    time.sleep(0.1)
    v_after = measure_node_adc(w2_row)
    print(f"  Step2 (+W1->CH1_POS, W1=+1.5V):             W2 ADC={v_after:+.4f}V  (expect ~-1.0V)")

    # Step 3: reverse order (W1 first)
    asyncio.run(awg_stop_all())
    jumperless.nodes_clear()
    asyncio.run(awg_set(1, 1.5))
    asyncio.run(awg_set(2, -1.0))
    jumperless.connect(w1_row, ch1_pos_row)
    jumperless.connect(ch2_neg_row, "GND")
    jumperless.connect(w2_row, ch2_pos_row)
    time.sleep(0.1)
    v_rev = measure_node_adc(w2_row)
    w2_path_rev = jumperless.eval_json(f"get_path_between({w2_row}, {ch2_pos_row})")
    print(f"  Step3 (reverse order, W1 first):             W2 ADC={v_rev:+.4f}V")
    print(f"    W2->CH2_POS path: {w2_path_rev}")

    # Step 4: W1=W2=+1.5V (same voltage — no fight if shorted)
    asyncio.run(awg_stop_all())
    jumperless.nodes_clear()
    asyncio.run(awg_set(1, 1.5))
    asyncio.run(awg_set(2, 1.5))
    jumperless.connect(w2_row, ch2_pos_row)
    jumperless.connect(ch2_neg_row, "GND")
    jumperless.connect(w1_row, ch1_pos_row)
    time.sleep(0.1)
    v_same = measure_node_adc(w2_row)
    print(f"  Step4 (W1=W2=+1.5V):                        W2 ADC={v_same:+.4f}V  (expect ~+1.5V if short OR correct)")

    asyncio.run(awg_stop_all())
    jumperless.nodes_clear()


@pytest.mark.hardware
def test_jumperless_ch1gnd_ch2w2(app, jumperless) -> None:
    """Part H: CH1_POS->GND + W2->CH2_POS (avoids W1->CH1_POS routing conflict).

    Replaces the W1->CH1_POS connection with CH1_POS->GND so CH1 reads ~0V,
    while CH2 reads W2's voltage. This sidesteps the chip1-y4 conflict entirely.
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    from tests.hardware import pinout
    backend = app.device.backend
    ai = backend._device.analogIn

    ch1_pos_row = pinout.row("CH1_POS")
    w2_row = pinout.row("W2")
    ch2_pos_row = pinout.row("CH2_POS")
    ch2_neg_row = pinout.row("CH2_NEG")

    async def awg_set(ch: int, v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": ch, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": ch})

    async def awg_stop_all() -> None:
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("awg.stop", {"channel": 2})

    print("\n=== Part H: CH1_POS->GND + W2->CH2_POS (no W1->CH1_POS) ===")

    jumperless.nodes_clear()
    jumperless.connect(ch1_pos_row, "GND")   # tie CH1_POS to GND
    jumperless.connect(w2_row, ch2_pos_row)
    jumperless.connect(ch2_neg_row, "GND")
    time.sleep(0.1)

    paths = jumperless.eval_json("get_all_paths()")
    print("  Active paths:")
    for p in paths:
        print(f"    {p.get('node1')} <-> {p.get('node2')}  chips={p.get('chips')}  y={p.get('y')}")

    asyncio.run(awg_set(2, 0.0))
    time.sleep(0.05)

    _setup_ai_single(ai)
    _read_both(ai)  # warmup

    print("\n  W2 voltage scan (CH1_POS tied to GND):")
    for v in [-1.0, -0.5, 0.0, +0.5, +1.0]:
        asyncio.run(awg_set(2, v))
        time.sleep(0.1)
        _, ch2 = _read_both(ai)
        ok = abs(ch2 - v) < 0.3
        print(f"  W2={v:+.1f}V  CH2={ch2:+.4f}V  {'✓' if ok else '✗ FAIL'}")

    asyncio.run(awg_stop_all())
    jumperless.nodes_clear()
    ai.reset()


@pytest.mark.hardware
def test_record_mode_ch2_timing(app, jumperless) -> None:
    """Part I: Isolate whether the two-channel record mode failure is timing or software.

    Manually connects all three routes, then runs:
      1. Single-shot immediately (confirm connection is live)
      2. Record mode with channels=[2] only (confirm CH2 record works alone)
      3. Record mode with channels=[1,2] after 3s extra settle (does extra time help?)
      4. Record mode with channels=[1,2] immediately after single-shot (no extra wait)

    All use direct pydwf API to avoid MCP tool overhead.
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    from tests.hardware import pinout
    from pydwf import DwfAcquisitionMode, DwfAnalogCoupling, DwfTriggerSource, DwfState
    import numpy as np

    backend = app.device.backend
    ai = backend._device.analogIn

    w1_row = pinout.row("W1")
    ch1_pos_row = pinout.row("CH1_POS")
    w2_row = pinout.row("W2")
    ch2_pos_row = pinout.row("CH2_POS")
    ch2_neg_row = pinout.row("CH2_NEG")

    async def awg_set(ch: int, v: float) -> None:
        await app.call_tool("awg.configure", {
            "channel": ch, "function": "DC", "frequency_hz": 1000.0,
            "amplitude_v": 0.0, "offset_v": v, "phase_deg": 0.0,
        })
        await app.call_tool("awg.start", {"channel": ch})

    async def awg_stop_all() -> None:
        await app.call_tool("awg.stop", {"channel": 1})
        await app.call_tool("awg.stop", {"channel": 2})

    def setup_single_shot():
        ai.reset()
        for ch in (0, 1):
            ai.channelEnableSet(ch, True)
            ai.channelRangeSet(ch, 5.0)
            ai.channelOffsetSet(ch, 0.0)
            ai.channelCouplingSet(ch, DwfAnalogCoupling.DC)
        ai.frequencySet(100_000.0)
        ai.bufferSizeSet(512)
        ai.acquisitionModeSet(DwfAcquisitionMode.Single)
        ai.triggerSourceSet(DwfTriggerSource.None_)

    def read_ch2_single_shot() -> float:
        ai.configure(False, True)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if ai.status(True) == DwfState.Done:
                break
            time.sleep(0.001)
        s = ai.statusData(1, 512)
        return float(sum(s) / len(s))

    def record_and_read_ch2(channels_1indexed: list[int], duration_s: float = 0.1) -> dict:
        """Run record mode and return mean CH2 from all captured samples."""
        ai.reset()
        for ch in (0, 1):
            ai.channelEnableSet(ch, (ch + 1) in channels_1indexed)
            ai.channelRangeSet(ch, 5.0)
            ai.channelOffsetSet(ch, 0.0)
            ai.channelCouplingSet(ch, DwfAnalogCoupling.DC)
        ai.frequencySet(50_000.0)
        ai.acquisitionModeSet(DwfAcquisitionMode.Record)
        ai.recordLengthSet(duration_s)
        ai.configure(False, True)

        ch1_chunks = []
        ch2_chunks = []
        n_total = int(50_000 * duration_s)
        n_read = 0
        deadline = time.monotonic() + duration_s + 1.0

        while n_read < n_total and time.monotonic() < deadline:
            ai.status(True)
            avail, lost, remaining = ai.statusRecord()
            if avail > 0:
                count = min(avail, n_total - n_read)
                d0 = ai.statusData(0, count)
                d1 = ai.statusData(1, count)
                if 0 in [c - 1 for c in channels_1indexed]:
                    ch1_chunks.extend(d0)
                if 1 in [c - 1 for c in channels_1indexed]:
                    ch2_chunks.extend(d1)
                n_read += count
            if remaining == 0:
                break
            time.sleep(0.005)

        ai.reset()
        result = {"n_samples": n_read}
        if ch1_chunks:
            result["ch1_mean"] = float(np.mean(ch1_chunks))
        if ch2_chunks:
            result["ch2_mean"] = float(np.mean(ch2_chunks))
        return result

    print("\n=== Part I: Record mode timing/isolation test ===")
    print(f"  W1=row{w1_row}->CH1_POS=row{ch1_pos_row}, W2=row{w2_row}->CH2_POS=row{ch2_pos_row}, "
          f"CH2_NEG=row{ch2_neg_row}->GND")

    # Establish all connections
    jumperless.nodes_clear()
    asyncio.run(awg_set(1, 1.5))
    asyncio.run(awg_set(2, -1.0))
    jumperless.connect(w1_row, ch1_pos_row)
    jumperless.connect(w2_row, ch2_pos_row)
    jumperless.connect(ch2_neg_row, "GND")
    time.sleep(0.3)

    # Step 1: Single-shot to confirm connections are live
    setup_single_shot()
    ch2_ss = read_ch2_single_shot()
    ok_ss = abs(ch2_ss - (-1.0)) < 0.3
    print(f"\n  [1] Single-shot CH2={ch2_ss:+.4f}V  {'✓' if ok_ss else '✗ UNEXPECTED'}")

    # Step 2: Record mode channels=[2] only
    r2 = record_and_read_ch2([2], duration_s=0.1)
    ok2 = abs(r2.get("ch2_mean", 999) - (-1.0)) < 0.3
    print(f"  [2] Record mode channels=[2]:  CH2={r2.get('ch2_mean', 'N/A'):+.4f}V  n={r2['n_samples']}  {'✓' if ok2 else '✗'}")

    # Step 3: Record mode channels=[1,2] immediately
    r12_fast = record_and_read_ch2([1, 2], duration_s=0.1)
    ok12f = abs(r12_fast.get("ch2_mean", 999) - (-1.0)) < 0.3
    print(f"  [3] Record mode channels=[1,2] (fast): "
          f"CH1={r12_fast.get('ch1_mean', 'N/A'):+.4f}V  CH2={r12_fast.get('ch2_mean', 'N/A'):+.4f}V  "
          f"n={r12_fast['n_samples']}  CH2={'✓' if ok12f else '✗'}")

    # Step 4: Record mode channels=[1,2] after extra 3s wait
    time.sleep(3.0)
    r12_slow = record_and_read_ch2([1, 2], duration_s=0.1)
    ok12s = abs(r12_slow.get("ch2_mean", 999) - (-1.0)) < 0.3
    print(f"  [4] Record mode channels=[1,2] (3s wait): "
          f"CH1={r12_slow.get('ch1_mean', 'N/A'):+.4f}V  CH2={r12_slow.get('ch2_mean', 'N/A'):+.4f}V  "
          f"n={r12_slow['n_samples']}  CH2={'✓' if ok12s else '✗'}")

    asyncio.run(awg_stop_all())
    jumperless.nodes_clear()
    ai.reset()

    # Print diagnosis
    print("\n  Diagnosis:")
    if ok_ss and ok2 and ok12f:
        print("  → All pass: record mode two-channel works! Issue was intermittent.")
    elif ok_ss and ok2 and not ok12f:
        print("  → Single-shot ✓, CH2-only record ✓, two-channel record ✗:")
        print("    The issue is in two-channel record mode specifically (CH1 read affects CH2).")
    elif ok_ss and not ok2:
        print("  → Single-shot ✓, CH2-only record ✗:")
        print("    The issue is in record mode itself, not specific to two-channel.")
    elif not ok_ss:
        print("  → Single-shot ✗: connections not established!")


@pytest.mark.hardware
def test_jumperless_routing_inspect(app, jumperless) -> None:
    """Part E: Inspect CH446Q routing paths for each connection combo.

    Prints exactly which chips and coordinates each connection uses, to identify
    shared resources between W1->CH1_POS and W2->CH2_POS.
    """
    if jumperless is None:
        pytest.skip("Jumperless not available")

    from tests.hardware import pinout

    w1_row = pinout.row("W1")
    ch1_pos_row = pinout.row("CH1_POS")
    w2_row = pinout.row("W2")
    ch2_pos_row = pinout.row("CH2_POS")
    ch2_neg_row = pinout.row("CH2_NEG")

    def dump_paths(label: str) -> None:
        paths = jumperless.eval_json("get_all_paths()")
        print(f"\n  [{label}]  {len(paths)} path(s):")
        for p in paths:
            chips = p.get("chips", "?")
            x = p.get("x", "?")
            y = p.get("y", "?")
            net = p.get("net", "?")
            dup = p.get("duplicate", False)
            n1, n2 = p.get("node1", "?"), p.get("node2", "?")
            print(f"    {n1} <-> {n2}  chips={chips}  x={x}  y={y}  net={net}  dup={dup}")

    print("\n=== Part E: CH446Q routing path inspection ===")
    print(f"  rows: W1={w1_row}, CH1_POS={ch1_pos_row}, W2={w2_row}, CH2_POS={ch2_pos_row}, CH2_NEG={ch2_neg_row}")

    # Step 1: W2->CH2_POS alone
    jumperless.nodes_clear()
    jumperless.connect(w2_row, ch2_pos_row)
    dump_paths(f"W2(row{w2_row})->CH2_POS(row{ch2_pos_row}) only")

    # Step 2: add CH2_NEG->GND
    jumperless.connect(ch2_neg_row, "GND")
    dump_paths(f"+CH2_NEG(row{ch2_neg_row})->GND")

    # Step 3: add W1->CH1_POS (this is where conflict appears)
    jumperless.connect(w1_row, ch1_pos_row)
    dump_paths(f"+W1(row{w1_row})->CH1_POS(row{ch1_pos_row})  <-- CONFLICT?")

    # Also query specific paths of interest
    p_w2_ch2 = jumperless.eval_json(f"get_path_between({w2_row}, {ch2_pos_row})")
    p_w1_ch1 = jumperless.eval_json(f"get_path_between({w1_row}, {ch1_pos_row})")
    print(f"\n  path W2->CH2_POS: {p_w2_ch2}")
    print(f"  path W1->CH1_POS: {p_w1_ch1}")

    jumperless.nodes_clear()
