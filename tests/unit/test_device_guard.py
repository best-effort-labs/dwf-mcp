"""Unit tests for the device-profile skip guard helper in tests/hardware/conftest.py."""
from __future__ import annotations

from tests.hardware.conftest import _devid_skip_reason


class _Marker:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _Node:
    def __init__(self, marker):
        self._marker = marker

    def get_closest_marker(self, name):
        return self._marker if name == "device" else None


class _Request:
    def __init__(self, marker):
        self.node = _Node(marker)


class _Profile:
    def __init__(self, devid):
        self.devid = devid


class _Dev:
    def __init__(self, devid):
        self.profile = _Profile(devid) if devid is not None else None


def test_no_device_marker_never_skips():
    assert _devid_skip_reason(_Request(None), _Dev(10)) is None


def test_matching_devid_does_not_skip():
    assert _devid_skip_reason(_Request(_Marker(devid=4)), _Dev(4)) is None


def test_mismatched_devid_returns_reason():
    reason = _devid_skip_reason(_Request(_Marker(devid=4)), _Dev(10))
    assert reason is not None
    assert "4" in reason and "10" in reason


def test_missing_profile_returns_reason():
    reason = _devid_skip_reason(_Request(_Marker(devid=4)), _Dev(None))
    assert reason is not None
    assert "4" in reason


def test_positional_devid_is_honored():
    # @pytest.mark.device(4) — positional form must filter just like devid=4
    assert _devid_skip_reason(_Request(_Marker(4)), _Dev(10)) is not None
    assert _devid_skip_reason(_Request(_Marker(4)), _Dev(4)) is None
