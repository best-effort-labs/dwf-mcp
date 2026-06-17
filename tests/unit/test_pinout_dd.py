"""Unit tests for the Digital Discovery wiring map in tests/hardware/pinout.py.

These reload the module so DD_REVERSED can be exercised from the environment.
"""
from __future__ import annotations

import importlib


def test_dd_default_rows() -> None:
    from tests.hardware import pinout
    importlib.reload(pinout)
    assert pinout.row("DIO24") == 6
    assert pinout.row("DIO25") == 5
    assert pinout.row("DIO26") == 4
    assert pinout.row("DIO27") == 3
    assert pinout.row("DD_GND") == 2
    assert pinout.row("DD_VIO") == 1
    assert pinout.row("DIO28") == 55
    assert pinout.row("DIO29") == 56
    assert pinout.row("DIO30") == 57
    assert pinout.row("DIO31") == 58


def test_dd_reversed_rows(monkeypatch) -> None:
    monkeypatch.setenv("DD_REVERSED", "1")
    from tests.hardware import pinout
    importlib.reload(pinout)
    try:
        assert pinout.row("DD_VIO") == 6
        assert pinout.row("DIO24") == 1
        assert pinout.row("DIO28") == 60
        assert pinout.row("DD_VIO_B") == 55
    finally:
        monkeypatch.delenv("DD_REVERSED", raising=False)
        importlib.reload(pinout)  # restore module to default-env state for other tests
