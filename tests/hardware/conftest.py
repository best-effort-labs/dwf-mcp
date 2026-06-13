from __future__ import annotations

import asyncio
import os
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
    # Target a specific connected device by serial via DWF_TEST_SERIAL; otherwise
    # open whichever device enumerates first. Useful with several Discoverys
    # attached (only one can be wired to the Jumperless at a time).
    serial = os.environ.get("DWF_TEST_SERIAL")
    open_args = {"device_serial": serial} if serial else {}
    asyncio.run(a.call_tool("waveforms.open", open_args))
    try:
        yield a
    finally:
        asyncio.run(a.call_tool("waveforms.close", {}))


@pytest.fixture
def device(tmp_path):
    """An opened DwfDevice on the DUT (honors DWF_TEST_SERIAL), with a permissive
    safety policy so functional hardware tests aren't blocked by caps. Instrument
    hardware tests must use this instead of constructing their own device, so every
    hardware test runs on the *same* selected device (not whichever enumerates
    first)."""
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.policy import SafetyPolicy

    dev = DwfDevice(
        backend=PydwfBackend(),
        policy=SafetyPolicy(
            supply_max_voltage_pos=5.0, supply_max_voltage_neg=-5.0,
            supply_max_current=1.0, awg_max_amplitude=5.0,
        ),
        allocator=PinAllocator(),  # configured from the device profile at open
        workspace=tmp_path, idle_timeout_s=60,
    )
    dev.open(serial=os.environ.get("DWF_TEST_SERIAL"))
    try:
        yield dev
    finally:
        dev.close()


@pytest.fixture
def artifacts(device):
    from dwf_mcp.artifacts import ArtifactWriter
    return ArtifactWriter(workspace=device.workspace)


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
