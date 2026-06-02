from __future__ import annotations

import pytest


@pytest.mark.hardware
def test_real_ad3_enumerate_and_open() -> None:
    from dwf_mcp.backends.pydwf_backend import PydwfBackend

    backend = PydwfBackend()
    devices = backend.enumerate()
    assert any(d.model.startswith("Analog Discovery") for d in devices), devices

    info = backend.open()
    try:
        assert info.serial
        assert backend.is_open
    finally:
        backend.close()
    assert not backend.is_open
