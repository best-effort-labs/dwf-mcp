"""UART sniff hardware test.

Stimulus options:
  A) External USB-UART adapter TX → DIO0 (most reliable)
  B) RP2350B via GPIO_1 (GP20) — NOT reliable: the Jumperless firmware periodically
     reconfigures GPIO_1-8 as part of its background crossbar management (~52ms cycle),
     which interrupts UART1 TX output on GP20. Use an external adapter instead.

NOTE on AD3 UART polarity: the AD3 protocol.uart with polaritySet(0) uses RS-232-like
physical convention (idle=LOW at the DIO pin, start=HIGH). An external device must also
use this convention — i.e. standard RS-232 levels, not TTL. A USB-UART adapter with
active-low idle (most RS-232 adapters) will work directly. A 3.3V TTL adapter requires
polaritySet(1) on the sniff side.

Run when external adapter is available:
  pytest tests/hardware/test_sniff_uart_hardware.py -v -m hardware
"""
import pytest


@pytest.mark.hardware
def test_sniff_uart_external_adapter() -> None:
    pytest.skip(
        "Requires external UART adapter TX → DIO0 (AD3 UART uses RS-232-like polarity; "
        "RP2350B GPIO_1-8 unreliable for UART — firmware periodically reconfigures them)"
    )
