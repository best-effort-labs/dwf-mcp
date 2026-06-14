from __future__ import annotations

from dwf_mcp.backend import DeviceInfo


def test_device_info_carries_devid_and_capabilities() -> None:
    info = DeviceInfo(
        serial="X", model="Analog Discovery 3", firmware="",
        devid=10,
        sample_rate_max_hz=100_000_000.0,
        dio_count=16, analog_in_channels=2, analog_out_channels=4,
        analog_in_buffer_max=16384, digital_in_buffer_max=16384,
        digital_word_width=16,
    )
    assert info.devid == 10
    assert info.analog_in_buffer_max == 16384
    assert info.digital_word_width == 16
