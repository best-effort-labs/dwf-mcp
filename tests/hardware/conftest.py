from __future__ import annotations

import asyncio
import contextlib
import os
import time
import warnings
from dataclasses import dataclass

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


def _open_args(request: pytest.FixtureRequest) -> dict:
    """Open args honoring DWF_TEST_SERIAL (which device) and a @device_config
    marker (which hardware config strategy, e.g. 'max_digital_in')."""
    args: dict = {}
    serial = os.environ.get("DWF_TEST_SERIAL")
    if serial:
        args["device_serial"] = serial
    marker = request.node.get_closest_marker("device_config")
    if marker:
        args["device_config"] = marker.args[0]
    return args


@dataclass(frozen=True)
class DutCaps:
    devid: int
    instruments: frozenset
    inventory: object  # PinInventory


def _requires_skip_reason(request: pytest.FixtureRequest, caps) -> str | None:
    """Skip reason if the test's @requires marker isn't satisfied by the DUT, else None."""
    marker = request.node.get_closest_marker("requires")
    if marker is None:
        return None
    if caps is None:
        return "no DUT available (probe-open failed)"
    need_instr = set(marker.kwargs.get("instruments", ()))
    need_pins = set(marker.kwargs.get("pins", ()))
    missing_i = sorted(need_instr - set(caps.instruments))
    missing_p = sorted(p for p in need_pins if not caps.inventory.is_valid_physical_pin(p))
    if missing_i or missing_p:
        return f"DUT lacks instrument(s) {missing_i} / pin(s) {missing_p}"
    return None



@pytest.fixture
def app(request):
    a = build_app(backend_name="pydwf")
    asyncio.run(a.call_tool("waveforms.open", _open_args(request)))
    try:
        yield a
    finally:
        asyncio.run(a.call_tool("waveforms.close", {}))


@pytest.fixture
def device(tmp_path, request):
    """An opened DwfDevice on the DUT (honors DWF_TEST_SERIAL and a @device_config
    marker), with a permissive safety policy so functional hardware tests aren't
    blocked by caps. Instrument hardware tests must use this instead of constructing
    their own device, so every hardware test runs on the *same* selected device."""
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
    args = _open_args(request)
    dev.open(serial=args.get("device_serial"), device_config=args.get("device_config"))
    try:
        yield dev
    finally:
        dev.close()


@pytest.fixture
def artifacts(device):
    from dwf_mcp.artifacts import ArtifactWriter
    return ArtifactWriter(workspace=device.workspace)


@pytest.fixture(scope="session")
def dut_caps(tmp_path_factory):
    """Probe-open the selected DUT once, cache its capabilities, close. None on failure."""
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.policy import SafetyPolicy

    dev = DwfDevice(
        backend=PydwfBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=5.0, supply_max_voltage_neg=-5.0,
                            supply_max_current=1.0, awg_max_amplitude=5.0),
        allocator=PinAllocator(),
        workspace=tmp_path_factory.mktemp("dut_probe"), idle_timeout_s=60,
    )
    caps = None
    try:
        dev.open(serial=os.environ.get("DWF_TEST_SERIAL"))
        if dev.inventory is not None and dev.profile is not None:
            caps = DutCaps(dev.profile.devid, dev.profile.supported_instruments, dev.inventory)
    except Exception:
        caps = None
    finally:
        with contextlib.suppress(Exception):
            dev.close()
    yield caps


@pytest.fixture(autouse=True)
def _require(request, dut_caps):
    reason = _requires_skip_reason(request, dut_caps)
    if reason:
        pytest.skip(reason)


@contextlib.contextmanager
def route_connections(jumperless, connections, *, skip_prompts):
    """Program (and on exit clear) a set of Jumperless connections. Shared by the
    `wire` (marker-driven) and `digital_loopback` (descriptor-driven) fixtures."""
    if jumperless is not None:
        jumperless.nodes_clear()
        for n1, n2 in connections.values():
            jumperless.connect(pinout.row(n1), pinout.row(n2))
            time.sleep(0.3)  # CH446Q programming settle per route
        time.sleep(0.1)
        try:
            yield
        finally:
            jumperless.nodes_clear()
    elif skip_prompts:
        yield
    else:
        for label, (n1, n2) in connections.items():
            input(f"  [{label}]  connect {n1} → {n2}, then press Enter ... ")
        try:
            yield
        finally:
            input("  Test done — remove connections, press Enter ... ")


# Per-device digital loopback: output pin -> input pin, with the device's GND reference.
# Keys are devid. sig_* are pinout.py signal names; out/inp are device pin names.
_DIGITAL_LOOPBACK = {
    10: dict(out="dio0",  inp="dio1",  sig_out="DIO0",  sig_in="DIO1",  gnd="AD3_GND"),
    4:  dict(out="dio24", inp="dio25", sig_out="DIO24", sig_in="DIO25", gnd="DD_GND"),
}


@pytest.fixture
def digital_loopback(request, dut_caps, jumperless, pytestconfig, _require):
    """Yield a (out_pin, in_pin) DIO pair valid for the connected DUT and wire it
    (out<->in + DUT GND <-> Jumperless GND). Portable across devices."""
    if request.node.get_closest_marker("jumperless") is not None:
        raise RuntimeError("digital_loopback tests must not also use @pytest.mark.jumperless")
    if dut_caps is None:
        pytest.skip("no DUT available")
    spec = _DIGITAL_LOOPBACK.get(dut_caps.devid)
    if spec is None:
        pytest.skip(f"no digital-loopback descriptor for devid {dut_caps.devid}")
    conns = {"loopback": (spec["sig_out"], spec["sig_in"]), "gnd": (spec["gnd"], "GND")}
    with route_connections(jumperless, conns,
                           skip_prompts=pytestconfig.getoption("--skip-wiring-prompts")):
        yield (spec["out"], spec["inp"])


@pytest.fixture(autouse=True)
def wire(request: pytest.FixtureRequest, jumperless, pytestconfig: pytest.Config, _require):
    marker = request.node.get_closest_marker("jumperless")
    if marker is None:
        yield
        return

    connections = marker.kwargs.get("connections", {})
    with route_connections(jumperless, connections,
                           skip_prompts=pytestconfig.getoption("--skip-wiring-prompts")):
        yield
