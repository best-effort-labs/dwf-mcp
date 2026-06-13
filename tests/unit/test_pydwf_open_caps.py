from __future__ import annotations

from types import SimpleNamespace

from dwf_mcp.backends.pydwf_backend import PydwfBackend


class _Enum:
    def enumerateDevices(self, _f): return 1
    def serialNumber(self, _i): return "SN123"
    def deviceName(self, _i): return "Analog Discovery 3"
    def deviceType(self, _i): return (10, 3)  # (devid, hwrev)
    def enumerateConfigurations(self, _i): return 1
    def configInfo(self, _c, _info): return 16384


def _fake_device():
    ai = SimpleNamespace(channelCount=lambda: 2, bitsInfo=lambda: 14,
                         bufferSizeInfo=lambda: (16, 16384),
                         frequencyInfo=lambda: (0.05, 100_000_000.0))
    ao = SimpleNamespace(count=lambda: 4)
    di = SimpleNamespace(bitsInfo=lambda: 16, bufferSizeInfo=lambda: 16384,
                         internalClockInfo=lambda: 100_000_000.0)
    return SimpleNamespace(analogIn=ai, analogOut=ao, digitalIn=di)


def test_open_populates_caps_from_device() -> None:
    # Bypass __init__ (which calls the real DwfLibrary) so this stays a pure unit test.
    b = object.__new__(PydwfBackend)
    b._device = None
    b._info = None
    b._spi_cs_idx = None
    b._ENUM_FILTER = 0
    b._dwf = SimpleNamespace(
        deviceEnum=_Enum(),
        deviceControl=SimpleNamespace(open=lambda i, c=None: _fake_device(),
                                      closeAll=lambda: None),
        getVersion=lambda: "fw")
    info = b.open()
    assert info.devid == 10
    assert info.sample_rate_max_hz == 100_000_000.0
    assert info.analog_in_buffer_max == 16384
    assert info.digital_in_buffer_max == 16384
    assert info.digital_word_width == 16
    assert info.analog_out_channels == 4
