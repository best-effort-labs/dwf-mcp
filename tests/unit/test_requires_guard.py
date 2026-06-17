"""Unit tests for the requirements-gating predicate in tests/hardware/conftest.py."""
from __future__ import annotations

from tests.hardware.conftest import DutCaps, _requires_skip_reason


class _Marker:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Node:
    def __init__(self, marker):
        self._marker = marker

    def get_closest_marker(self, name):
        return self._marker if name == "requires" else None


class _Request:
    def __init__(self, marker):
        self.node = _Node(marker)


class _Inv:
    def __init__(self, pins):
        self._pins = set(pins)

    def is_valid_physical_pin(self, pin):
        return pin in self._pins


def _caps(instruments, pins):
    return DutCaps(devid=10, instruments=frozenset(instruments), inventory=_Inv(pins))


def test_no_marker_never_skips():
    assert _requires_skip_reason(_Request(None), _caps({"scope"}, {"scope1"})) is None


def test_satisfied_does_not_skip():
    req = _Request(_Marker(instruments={"scope"}, pins={"scope1"}))
    assert _requires_skip_reason(req, _caps({"scope"}, {"scope1"})) is None


def test_missing_instrument_skips():
    req = _Request(_Marker(instruments={"scope"}))
    reason = _requires_skip_reason(req, _caps({"logic"}, set()))
    assert reason and "scope" in reason


def test_missing_pin_skips():
    req = _Request(_Marker(pins={"dio0"}))
    reason = _requires_skip_reason(req, _caps({"logic"}, {"dio24"}))
    assert reason and "dio0" in reason


def test_none_caps_with_marker_skips():
    req = _Request(_Marker(instruments={"logic"}))
    assert _requires_skip_reason(req, None) is not None


def test_none_caps_without_marker_does_not_skip():
    assert _requires_skip_reason(_Request(None), None) is None
