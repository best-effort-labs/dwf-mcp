from __future__ import annotations

import asyncio
import time
import warnings

import pytest

from dwf_mcp.server import build_app
from tests.hardware import pinout


def wait_for_sniff_claim(app, instrument_key: str, timeout_s: float = 1.5,
                         setup_grace_s: float = 0.05) -> None:
    """Block until `instrument_key` appears in the allocator's claimed instruments,
    then sleep `setup_grace_s` so the spy/configure path can finish arming.

    Used by sniff hardware tests to synchronize the main thread (firing stimulus)
    with the background sniff thread (configuring the spy) without a fixed sleep.
    Raises TimeoutError if the claim never appears.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if instrument_key in app.device.allocator.claimed_instruments():
            time.sleep(setup_grace_s)
            return
        time.sleep(0.005)
    raise TimeoutError(
        f"sniff thread did not claim {instrument_key!r} within {timeout_s}s"
    )


@pytest.fixture(scope="session")
def jumperless(pytestconfig: pytest.Config):
    if pytestconfig.getoption("--skip-wiring-prompts"):
        yield None
        return
    try:
        from jlv5_harness import Harness, find_ports
    except ImportError:
        yield None
        return
    try:
        ports = find_ports()
        if len(ports) < 3 or pytestconfig.getoption("--jumperless-manual"):
            yield None
            return
        j = Harness()
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


@pytest.fixture
def app():
    a = build_app(backend_name="pydwf")
    asyncio.run(a.call_tool("waveforms.open", {}))
    try:
        yield a
    finally:
        asyncio.run(a.call_tool("waveforms.close", {}))


@pytest.fixture(autouse=True)
def wire(request: pytest.FixtureRequest, jumperless, pytestconfig: pytest.Config):
    marker = request.node.get_closest_marker("jumperless")
    if marker is None:
        yield
        return

    connections: dict[str, tuple[str, str]] = marker.kwargs.get("connections", {})
    skip = pytestconfig.getoption("--skip-wiring-prompts")

    if jumperless is not None:
        jumperless.nodes_clear()
        for n1, n2 in connections.values():
            jumperless.connect(pinout.row(n1), pinout.row(n2))
            time.sleep(0.3)  # allow CH446Q firmware to fully program each route before the next
        time.sleep(0.1)  # final settle
        try:
            yield
        finally:
            jumperless.nodes_clear()
    elif skip:
        yield
    else:
        for label, (n1, n2) in connections.items():
            input(f"  [{label}]  connect {n1} → {n2}, then press Enter ... ")
        try:
            yield
        finally:
            input("  Test done — remove connections, press Enter ... ")
