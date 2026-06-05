from __future__ import annotations

import warnings

import pytest


@pytest.fixture(scope="session")
def jumperless(pytestconfig: pytest.Config):
    if pytestconfig.getoption("--skip-wiring-prompts"):
        yield None
        return
    try:
        from jumperless import Jumperless, find_jumperless_ports
    except ImportError:
        yield None
        return
    try:
        ports = find_jumperless_ports()
        if len(ports) < 3 or pytestconfig.getoption("--jumperless-manual"):
            yield None
            return
        j = Jumperless()
    except Exception as exc:
        warnings.warn(
            f"Jumperless probe/open failed ({exc!r}), falling back to manual prompts",
            UserWarning,
            stacklevel=2,
        )
        yield None
        return
    try:
        yield j
    finally:
        j.close()
