#!/usr/bin/env python3
"""CAN diagnostic 2 — decode CAN TX output to understand polarity and test Pattern Generator RX.

Usage: pytest tests/hardware/diag_can2.py -v --no-header -m hardware -s
"""
from __future__ import annotations
import time
import ctypes
import pytest


def crc15(bits):
    crc, gen = 0, 0x4599
    for b in bits:
        if b ^ ((crc >> 14) & 1):
            crc = ((crc << 1) & 0x7FFF) ^ gen
        else:
            crc = (crc << 1) & 0x7FFF
    return crc


def stuff(bits):
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        if b == last:
            run += 1
            if run == 5:
                out.append(1 - b)
                run, last = 0, 1 - b
                continue
        else:
            run = 1
        last = b
    return out


def can_frame_gpio(can_id, data, dom_high=False):
    """Build CAN 2.0A frame as GPIO bit list. dom_high=True → idle=LOW, SOF=HIGH."""
    D = 1 if dom_high else 0  # dominant level
    R = 0 if dom_high else 1  # recessive level

    id_bits = [(can_id >> i) & 1 for i in range(10, -1, -1)]
    dlc_bits = [(len(data) >> i) & 1 for i in range(3, -1, -1)]
    data_bits = []
    for byte in data:
        data_bits += [(byte >> i) & 1 for i in range(7, -1, -1)]

    # logical (0=dominant, 1=recessive) pre-CRC
    logical = [0] + id_bits + [0, 0, 0] + dlc_bits + data_bits
    crc = crc15(logical)
    crc_bits = [(crc >> i) & 1 for i in range(14, -1, -1)]
    logical += crc_bits

    stuffed = stuff(logical)
    gpio = [D if b == 0 else R for b in stuffed]

    # Unstuffed tail
    gpio += [R]      # CRC delimiter
    gpio += [D]      # ACK slot (we drive dominant for self-test)
    gpio += [R]      # ACK delimiter
    gpio += [R] * 7  # EOF
    gpio += [R] * 11 # idle

    return gpio


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_can_diag2(app) -> None:
    from pydwf import DwfState, DwfAcquisitionMode, DwfDigitalOutIdle, DwfDigitalOutType
    backend = app.device.backend
    device = backend._device
    can = device.protocol.can
    din = device.digitalIn
    dout = device.digitalOut

    print("\n=== CAN Diagnostic 2 ===")

    # ----------------------------------------------------------------
    # Part A: Decode what CAN TX actually outputs
    # ----------------------------------------------------------------
    BIT_RATE = 10_000
    SAMPLES_PER_BIT = 10  # capture at 10x bit rate = 100kHz
    SAMPLE_RATE = BIT_RATE * SAMPLES_PER_BIT

    print(f"\nA) Decode CAN TX output at {SAMPLE_RATE/1000:.0f}kHz ({SAMPLES_PER_BIT} samples/bit):")

    can.reset()
    can.rateSet(BIT_RATE)
    can.txSet(0)
    time.sleep(0.010)
    can.rxSet(1)
    can.rx()
    time.sleep(0.020)

    din.reset()
    din.inputOrderSet(False)
    clk = din.internalClockInfo()
    divider = max(1, round(clk / SAMPLE_RATE))
    actual_rate = clk / divider
    din.dividerSet(divider)
    NBUF = 4096
    din.bufferSizeSet(NBUF)
    din.acquisitionModeSet(DwfAcquisitionMode.Single)
    din.configure(False, True)

    can.tx(0x123, False, False, bytes([0x01, 0x02, 0x03]))

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        state = din.status(True)
        if state == DwfState.Done:
            break
        time.sleep(0.001)

    raw = din.statusData(NBUF, 16)
    raw_int = [int(s) for s in raw]
    dio0 = [(s >> 0) & 1 for s in raw_int]

    # Find where signal first goes to a different level
    first_val = dio0[0]
    first_change = next((i for i, b in enumerate(dio0) if b != first_val), None)
    print(f"  First {20} samples: {''.join(str(b) for b in dio0[:20])}")
    print(f"  Initial level: {first_val} ({'LOW' if first_val == 0 else 'HIGH'})")
    print(f"  First change at sample {first_change} ({(first_change or 0)*1000/actual_rate:.1f} µs)")

    # Identify bit-rate by looking at the first transition and assuming it's SOF
    # Downsample: take the middle sample of each bit period
    transitions = [i for i in range(1, NBUF) if dio0[i] != dio0[i-1]]
    print(f"  Transitions: {len(transitions)} at samples {transitions[:10]}")

    if first_change:
        idle_level = first_val
        dom_high = (idle_level == 0)  # if idle is LOW, then recessive=LOW → dom=HIGH
        print(f"  Detected polarity: dom={'HIGH' if dom_high else 'LOW'}")
        # Bit align: assume first_change is SOF start, each bit = SAMPLES_PER_BIT samples
        print(f"\n  Decoded bits (1 per {SAMPLES_PER_BIT} samples, starting at SOF):")
        decoded_bits = []
        for bit_idx in range(100):
            sample_idx = first_change + bit_idx * SAMPLES_PER_BIT + SAMPLES_PER_BIT // 2
            if sample_idx >= NBUF:
                break
            gpio_level = dio0[sample_idx]
            # Convert GPIO level to logical CAN bit
            logical = 0 if gpio_level == (1 if dom_high else 0) else 1  # 0=dominant, 1=recessive
            decoded_bits.append(logical)
        print(f"  Logical bits: {''.join(str(b) for b in decoded_bits[:50])}")
        # Expected first bits: SOF(0) + ID[10..0] for 0x123
        id_bits = [(0x123 >> i) & 1 for i in range(10, -1, -1)]
        expected = [0] + id_bits  # SOF + ID
        print(f"  Expected     : {''.join(str(b) for b in expected)}")
        match = decoded_bits[:len(expected)] == expected
        print(f"  SOF+ID match: {'✓' if match else '✗'}")

    din.reset()

    # ----------------------------------------------------------------
    # Part B: Pattern Generator on DIO0 → Jumperless → DIO1, CAN RX on DIO1
    # (No Protocol CAN TX — pure pattern generator test of RX decoder)
    # ----------------------------------------------------------------
    print("\n\nB) Pattern Gen(DIO0) → Jumperless → DIO1, CAN RX on DIO1:")

    for dom_high in [False, True]:
        polarity_name = "dom=HIGH" if dom_high else "dom=LOW"
        print(f"\n  Testing polarity {polarity_name}:")

        frame = can_frame_gpio(0x456, [0xAA, 0xBB], dom_high=dom_high)
        idle_level = 0 if dom_high else 1  # recessive level

        # 100 idle bits preamble so CAN RX can detect bus idle before frame
        preamble = [idle_level] * 100
        postamble = [idle_level] * 50
        full_signal = preamble + frame + postamble

        bits_str = ''.join(str(b) for b in full_signal)
        n_bits = len(full_signal)

        print(f"  Frame bits: {len(frame)}, total with preamble: {n_bits}")
        print(f"  Preamble first bit: {full_signal[0]}, frame start: {full_signal[100]}")

        # Configure DigitalOut on DIO0 first (so pin is stable before CAN RX starts)
        can.reset()
        dout.reset()

        node = 0  # DIO0 = pattern generator output
        dout.enableSet(node, True)
        dout.typeSet(node, DwfDigitalOutType.Custom)
        dout_idle = DwfDigitalOutIdle.High if idle_level == 1 else DwfDigitalOutIdle.Low
        dout.idleSet(node, dout_idle)
        dout_div = max(1, round(100_000_000 / BIT_RATE))
        dout.dividerSet(node, dout_div)
        dout.dataSet(node, bits_str)
        run_s = n_bits / BIT_RATE + 0.010
        dout.runSet(run_s)
        dout.repeatSet(1)

        # Start pattern generator (DIO0 now outputs idle level = recessive)
        dout.configure(True)
        time.sleep(0.005)  # tiny settle, then enable CAN RX

        # Now configure Protocol CAN RX on DIO1
        # (Pattern Generator preamble is already running on DIO0 → Jumperless → DIO1)
        can.rateSet(BIT_RATE)
        can.polaritySet(dom_high)
        can.rxSet(1)   # DIO1 = RX (via Jumperless from DIO0)
        can.rx()       # prime

        frame_start_s = 100 / BIT_RATE  # preamble duration
        wait_for_frame = frame_start_s + len(frame) / BIT_RATE + 0.200
        print(f"  Waiting {wait_for_frame:.3f}s for frame to be output...")
        time.sleep(wait_for_frame)

        # Poll for received frame
        got_any = False
        for i in range(30):
            v, e, r, d, s = can.rx()
            if d or s:
                print(f"  rx[{i}]: v={v:#x} ext={e} rtr={r} data={bytes(d).hex()!r} status={s}")
                if d:
                    got_any = True
                    print(f"  ✓ Decoded: id={v:#x} data={bytes(d).hex()!r}")
                    break
            time.sleep(0.005)

        if not got_any:
            print(f"  ✗ No frame decoded")

        dout.reset()

    can.reset()
