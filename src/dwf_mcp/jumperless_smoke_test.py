"""
Quick smoke test — run with the Jumperless plugged in:
    python -m dwf_mcp.jumperless_smoke_test
"""

from .jumperless import Jumperless, find_jumperless_ports

def main():
    ports = find_jumperless_ports()
    print(f"Found ports: {ports}")
    if len(ports) < 3:
        print("Need at least 3 ports for Raw REPL — is the Jumperless plugged in?")
        return

    with Jumperless() as j:
        print("Connected to Raw REPL")

        # Basic exec
        j.exec("nodes_clear()")
        print("Board cleared")

        # ADC read
        v = j.adc_get(0)
        print(f"ADC0: {v:.3f} V")

        # Connect + read
        j.connect(1, "ADC0")
        v = j.adc_get(0)
        print(f"ADC0 on row 1: {v:.3f} V")
        j.disconnect(1, "ADC0")

        # DAC round-trip
        j.dac_set("TOP_RAIL", 3.3)
        got = j.dac_get("TOP_RAIL")
        print(f"TOP_RAIL set 3.3 V, reads back: {got:.3f} V")

        # State snapshot
        state = j.get_state()
        print(f"State keys: {list(state.keys())}")

        # OLED
        j.oled_print("Smoke test OK")
        print("Done — 'Smoke test OK' should appear on OLED")

if __name__ == "__main__":
    main()
