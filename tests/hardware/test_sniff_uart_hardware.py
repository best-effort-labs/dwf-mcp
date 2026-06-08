"""UART sniff hardware test.
Requires external UART transmitter on DIO0 (sniff.uart resets the engine on entry,
making concurrent uart.write impossible from the same session).
External setup: USB-UART adapter TX → DIO0.
"""
import pytest


@pytest.mark.hardware
def test_sniff_uart_loopback() -> None:
    pytest.skip("requires external UART transmitter (USB-UART adapter TX → DIO0)")
