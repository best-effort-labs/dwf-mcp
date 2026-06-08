#!/usr/bin/env python3
"""CAN loopback diagnostic — runs inside pytest hardware fixture.
Usage: pytest tests/hardware/diag_can.py -v --no-header -m hardware -s
"""
from __future__ import annotations

import time

import numpy as np
import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_can_diag(app) -> None:
    """
    Two sub-tests:
    1. Logic capture: Does DIO1 actually see the CAN TX waveform from DIO0?
    2. CAN RX: Does the Protocol CAN decoder receive the frame?
    """
    from pydwf import DwfState
    backend = app.device.backend
    device = backend._device
    can = device.protocol.can
    din = device.digitalIn

    print("\n--- CAN diagnostic (DIO0=TX DIO1=RX via Jumperless) ---")

    # --- Test A: capture DIO1 while CAN TX fires ---
    print("\nA) Logic capture of DIO1 during CAN TX:")
    can.reset()
    can.rateSet(10_000)  # slow so we see individual bits at 1MHz sample rate
    can.txSet(0)
    time.sleep(0.010)
    can.rxSet(1)
    can.rx()
    time.sleep(0.020)

    # Set up logic capture of DIO1 at 1MHz
    din.reset()
    din.inputOrderSet(False)
    # Get valid divider info
    clk = din.internalClockInfo()
    divider = max(1, round(clk / 1_000_000))
    din.dividerSet(divider)
    din.bufferSizeSet(4096)
    from pydwf import DwfAcquisitionMode
    din.acquisitionModeSet(DwfAcquisitionMode.Single)
    din.configure(False, True)

    # Start CAN TX
    can.tx(0x123, False, False, bytes([0xAB, 0xCD]))

    # Wait for capture
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        state = din.status(True)
        if state == DwfState.Done:
            break
        time.sleep(0.001)

    raw = din.statusData(4096, 16)  # 4096 samples, 16-bit format
    raw_int = [int(s) for s in raw]
    dio0 = [(s >> 0) & 1 for s in raw_int]
    dio1 = [(s >> 1) & 1 for s in raw_int]
    t0 = sum(1 for i in range(1, len(dio0)) if dio0[i] != dio0[i-1])
    t1 = sum(1 for i in range(1, len(dio1)) if dio1[i] != dio1[i-1])
    print(f"  DIO0: {sum(dio0)} HIGH, {len(dio0)-sum(dio0)} LOW, {t0} transitions")
    print(f"  DIO1: {sum(dio1)} HIGH, {len(dio1)-sum(dio1)} LOW, {t1} transitions")
    print(f"  DIO0 first 60 bits: {''.join(str(b) for b in dio0[:60])}")
    print(f"  DIO1 first 60 bits: {''.join(str(b) for b in dio1[:60])}")
    # Find first 1 (HIGH) in DIO0
    first_high = next((i for i, b in enumerate(dio0) if b == 1), None)
    print(f"  DIO0 first HIGH at sample {first_high} ({(first_high or 0)/10:.1f} µs)")
    if t1 > 2:
        print("  ✓ CAN waveform IS reaching DIO1")
    else:
        print("  ✗ DIO1 stuck — no CAN waveform visible")

    din.reset()

    # --- Test B: CAN RX decoding, dom=LOW only, focus on timing ---
    print("\nB) Timing experiment — poll rx DURING and AFTER tx:")
    can.reset()
    can.rateSet(10_000)
    can.txSet(0)
    time.sleep(0.010)
    can.rxSet(1)
    can.rx()          # prime
    time.sleep(0.020)

    import threading
    frames_during = []
    stop_flag = threading.Event()

    def poll_rx():
        while not stop_flag.is_set():
            v, e, r, d, s = can.rx()
            frames_during.append((v, e, r, bytes(d), s))
            time.sleep(0.001)

    t = threading.Thread(target=poll_rx, daemon=True)
    t.start()
    time.sleep(0.020)  # let poll thread warm up

    print("  [tx] sending frame...")
    can.tx(0x123, False, False, bytes([0x01, 0x02, 0x03]))
    print("  [tx] returned")
    time.sleep(0.200)  # wait well past frame completion
    stop_flag.set()
    t.join(timeout=1.0)

    nonzero = [(v, e, r, d.hex(), s) for v, e, r, d, s in frames_during if s or d]
    print(f"  total polls: {len(frames_during)}")
    print(f"  nonzero frames: {len(nonzero)}")
    for f in nonzero[:5]:
        print(f"    {f}")
    if not nonzero:
        print("  ✗ no frame decoded in threaded polling")

    # --- Test C: send 3 frames, poll after each ---
    print("\nC) Send 3 frames sequentially, poll after each:")
    can.reset()
    can.rateSet(10_000)
    can.txSet(0)
    time.sleep(0.010)
    can.rxSet(1)
    can.rx()   # prime
    time.sleep(0.020)

    for seq in range(3):
        can.tx(0x100 + seq, False, False, bytes([seq, seq+1]))
        time.sleep(0.200)
        v, e, r, d, s = can.rx()
        print(f"  frame {seq}: v_id={v:#x} data={bytes(d).hex()!r} status={s}")

    # --- Test D: rx=DIO0 same-pin, poll DURING tx ---
    print("\nD) Same-pin loopback rx=DIO0, poll during tx:")
    can.reset()
    can.rateSet(10_000)
    can.txSet(0)
    time.sleep(0.010)
    can.rxSet(0)
    can.rx()
    time.sleep(0.020)

    frames_d = []
    stop2 = threading.Event()
    def poll_rx2():
        while not stop2.is_set():
            v, e, r, d, s = can.rx()
            frames_d.append((v, e, r, bytes(d), s))
            time.sleep(0.001)
    t2 = threading.Thread(target=poll_rx2, daemon=True)
    t2.start()
    time.sleep(0.020)
    can.tx(0x123, False, False, bytes([0xDE, 0xAD]))
    time.sleep(0.200)
    stop2.set()
    t2.join(timeout=1.0)
    nonzero2 = [(v, e, r, d.hex(), s) for v, e, r, d, s in frames_d if s or d]
    print(f"  total polls: {len(frames_d)}, nonzero: {len(nonzero2)}")
    for f in nonzero2[:3]:
        print(f"    {f}")
