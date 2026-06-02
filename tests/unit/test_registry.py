from __future__ import annotations

from typing import Any, ClassVar

import pytest

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.registry import InstrumentRegistry


class DummyInstrument(Instrument):
    name = "dummy"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "ping": ("ping", {"type": "object", "properties": {}}),
    }

    def __init__(self, device: object, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False

    def ping(self) -> dict[str, str]:
        return {"pong": "ok"}

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
        tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {}

        def __init__(self, device: object, artifacts: ArtifactWriter) -> None: ...
        def release(self) -> None: ...

    reg = InstrumentRegistry()
    with pytest.raises(TypeError):
        reg.register(Nameless)


def test_instrument_not_configured_is_exception() -> None:
    err = InstrumentNotConfigured("scope must be configured before capture")
    assert isinstance(err, Exception)
    assert "scope" in str(err)
