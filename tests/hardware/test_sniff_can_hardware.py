"""CAN sniff hardware test.
Requires external CAN transceiver and CAN node on DIO0 (RX).
"""
import pytest


@pytest.mark.hardware
def test_sniff_can_external_device() -> None:
    pytest.skip("requires external CAN device (RX=DIO0)")
