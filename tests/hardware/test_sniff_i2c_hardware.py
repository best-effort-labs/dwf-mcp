"""I2C sniff hardware test.
Requires external I2C device on SDA=DIO0, SCL=DIO1.
The I2C spy and active master share the same hardware engine on the AD3
and cannot coexist on a single device.
"""
import pytest


@pytest.mark.hardware
def test_sniff_i2c_external_device() -> None:
    pytest.skip("requires external I2C device (SDA=DIO0, SCL=DIO1)")
