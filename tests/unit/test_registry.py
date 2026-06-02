from __future__ import annotations

import pytest

from dwf_mcp.instrument import Instrument
from dwf_mcp.registry import InstrumentRegistry


class DummyInstrument(Instrument):
    name = "dummy"
    required_pins: list[str] = []

    def configure(self, **kwargs: object) -> None:
        self._configured = True

    def release(self) -> None:
        self._configured = False


def test_register_and_lookup() -> None:
    reg = InstrumentRegistry()
    reg.register(DummyInstrument)
    assert reg.get_class("dummy") is DummyInstrument
    assert "dummy" in reg.names()


def test_duplicate_registration_raises() -> None:
    reg = InstrumentRegistry()
    reg.register(DummyInstrument)
    with pytest.raises(ValueError):
        reg.register(DummyInstrument)


def test_unknown_instrument_raises() -> None:
    reg = InstrumentRegistry()
    with pytest.raises(KeyError):
        reg.get_class("missing")


def test_instrument_abc_requires_name() -> None:
    class Nameless(Instrument):  # type: ignore[misc]
        required_pins: list[str] = []
        def configure(self, **kwargs: object) -> None: ...
        def release(self) -> None: ...
    # name attribute missing should be flagged at registration time
    reg = InstrumentRegistry()
    with pytest.raises(TypeError):
        reg.register(Nameless)
