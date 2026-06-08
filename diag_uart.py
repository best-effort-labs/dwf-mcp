#!/usr/bin/env python3
"""UART loopback diagnostic — run with DIO0 wired to DIO1.

Tests whether stale-buffer or framing issues explain the Hello corruption.
Usage: python diag_uart.py
"""
import time

try:
    from pydwf import DwfLibrary
    from pydwf.utilities import openDwfDevice
except ImportError:
    print("pydwf not available")
    raise SystemExit(1)

BAUD = 9600
TX_PIN = 0   # DIO0
RX_PIN = 1   # DIO1


def setup_uart(uart, prime: bool = False):
    uart.reset()
    uart.rateSet(BAUD)
    uart.bitsSet(8)
    uart.paritySet(0)
    uart.stopSet(1)
    uart.txSet(TX_PIN)
    if prime:
        uart.tx(b"")  # force TX idle (HIGH) before enabling RX
    uart.rxSet(RX_PIN)
    uart.rx(0)   # initialize receiver
    if prime:
        uart.rx(1)   # activate DMA buffer


def flush_rx(uart, label=""):
    """Drain any stale bytes from the receive buffer."""
    stale = b""
    for _ in range(20):
        d, _ = uart.rx(64)
        if not d:
            break
        stale += bytes(d)
        time.sleep(0.002)
    if stale:
        print(f"  [flush{' ' + label if label else ''}] drained {len(stale)} stale bytes: {stale.hex()}")
    return stale


def roundtrip(uart, payload: bytes, label: str, flush_before: bool, polling: bool = False) -> bytes:
    if flush_before:
        flush_rx(uart, "before-tx")
    uart.tx(payload)
    bit_time_s = 1.0 / BAUD
    frame_bits = 1 + 8 + 1
    if polling:
        # Poll until we have the expected bytes or 1s timeout
        deadline = time.monotonic() + 1.0
        buf = b""
        while len(buf) < len(payload) and time.monotonic() < deadline:
            d, _ = uart.rx(len(payload) - len(buf))
            if d:
                buf += bytes(d)
            else:
                time.sleep(0.002)
        rx_bytes = buf
        parity_err = 0
    else:
        time.sleep(len(payload) * frame_bits * bit_time_s * 2 + 0.005)
        rx_data, parity_err = uart.rx(len(payload) + 4)
        rx_bytes = bytes(rx_data)
    match = "✓" if rx_bytes == payload else "✗"
    mode = " [poll]" if polling else ""
    print(f"  {match} {label}{mode}: tx={payload.hex()} rx={rx_bytes.hex()} parity_err={parity_err}")
    return rx_bytes


dwf = DwfLibrary()
with openDwfDevice(dwf) as device:
    uart = device.protocol.uart

    print("=== Test 1: known-good bytes, no prime ===")
    setup_uart(uart, prime=False)
    roundtrip(uart, b"AAAAA", "AAAAA no-prime", flush_before=False)
    roundtrip(uart, b"UUUUU", "UUUUU no-prime", flush_before=False)

    print("\n=== Test 2: Hello, no prime (baseline fail) ===")
    setup_uart(uart, prime=False)
    roundtrip(uart, b"Hello", "Hello no-prime", flush_before=False, polling=True)

    print("\n=== Test 3: Hello, WITH prime (expected fix) ===")
    setup_uart(uart, prime=True)
    roundtrip(uart, b"Hello", "Hello primed  ", flush_before=False, polling=True)
    roundtrip(uart, b"Hello", "Hello 2nd call", flush_before=False, polling=True)

    print("\n=== Test 3b: prime + 50ms delay before first TX ===")
    setup_uart(uart, prime=True)
    time.sleep(0.05)
    roundtrip(uart, b"Hello", "Hello prime+50ms", flush_before=False, polling=True)
    roundtrip(uart, b"Hello", "Hello 2nd call  ", flush_before=False, polling=True)

    print("\n=== Test 3b2: prime + 10ms delay before first TX ===")
    setup_uart(uart, prime=True)
    time.sleep(0.010)
    roundtrip(uart, b"Hello", "Hello prime+10ms", flush_before=False, polling=True)

    print("\n=== Test 3c: prime then send dummy + discard, then real TX ===")
    setup_uart(uart, prime=True)
    uart.tx(b'\x00')   # dummy byte, will be lost
    bit_time_s = 1.0 / BAUD
    time.sleep(10 * bit_time_s)   # one frame
    uart.rx(4)                     # discard
    roundtrip(uart, b"Hello", "Hello after dummy", flush_before=False, polling=True)

    print("\n=== Test 4: individual bytes (no pre-flush after setup) ===")
    setup_uart(uart)
    flush_rx(uart, "setup")
    for b in [0x41, 0x55, 0x48, 0x65, 0x6c, 0x6f, 0x00, 0xff, 0x01, 0x80]:
        roundtrip(uart, bytes([b]), f"0x{b:02x}", flush_before=True)

    print("\n=== Test 5: Hello repeated (check if first is stale, rest correct) ===")
    setup_uart(uart)
    # Do NOT flush — let stale bytes accumulate, then send multiple Hello
    for i in range(3):
        roundtrip(uart, b"Hello", f"Hello #{i}", flush_before=False)
