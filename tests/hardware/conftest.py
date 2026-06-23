from __future__ import annotations

import asyncio
import contextlib
import os
import time
import warnings
from dataclasses import dataclass

import pytest

from dwf_mcp.devices.inventory import PinInventory
from dwf_mcp.server import build_app
from tests.hardware import pinout


def _jumperless_present(config: pytest.Config) -> bool:
    """Whether a Jumperless looks attached (harness importable + >=3 ports), cached
    once per session. Independent of the wiring-mode flags — used to auto-skip wired
    tests when no board is connected (so `pytest -m hardware` is safe either way)."""
    cached = getattr(config, "_jl_present", None)
    if cached is None:
        try:
            from jlv5_harness import find_ports
            cached = len(find_ports()) >= 3
        except Exception:
            cached = False
        config._jl_present = cached  # type: ignore[attr-defined]
    return cached


def _uses_jumperless(item: pytest.Item) -> bool:
    """Whether a test drives the Jumperless crossbar (auto-wiring marker or the
    `digital_loopback` fixture). Distinct from the broader `wired` category, which
    also covers tests needing a manual cable the Jumperless can't route (e.g. BNC)."""
    return (
        item.get_closest_marker("jumperless") is not None
        or "digital_loopback" in getattr(item, "fixturenames", ())
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Tag every hardware test `wired` (needs external physical connections) or
    `standalone` (device-only), so the two sets can be toggled with `-m`. Jumperless
    tests are auto-detected as wired; a test needing a manual cable can mark itself
    `@pytest.mark.wired` and is respected here. Everything else under -m hardware is
    standalone. Auto-applied so new tests are classified without manual marking."""
    for item in items:
        if item.get_closest_marker("hardware") is None:
            continue
        # Respect an explicit classification (e.g. a manual-cable test marks itself
        # `wired` although it doesn't drive the Jumperless).
        if item.get_closest_marker("wired") or item.get_closest_marker("standalone"):
            continue
        item.add_marker(pytest.mark.wired if _uses_jumperless(item) else pytest.mark.standalone)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Jumperless-convenience skip: tests that drive the Jumperless auto-skip when no
    board is attached, so the full `-m hardware` run is safe with or without one.
    Overrides: --jumperless-manual (wire by hand) / --skip-wiring-prompts (pre-wired).
    Manual-cable wired tests aren't Jumperless tests — they self-skip via their own
    opt-in (e.g. an env flag) rather than here."""
    if not _uses_jumperless(item):
        return
    cfg = item.config
    if cfg.getoption("--skip-wiring-prompts") or cfg.getoption("--jumperless-manual"):
        return
    if not _jumperless_present(cfg):
        pytest.skip(
            "no Jumperless attached; wired test skipped "
            "(--jumperless-manual to wire by hand, --skip-wiring-prompts if pre-wired)"
        )


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
            f"Jumperless probe/open failed ({exc!r}); wired tests will skip "
            f"(use --jumperless-manual to wire by hand, --skip-wiring-prompts if pre-wired)",
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
    instruments: frozenset[str]
    inventory: PinInventory


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
def route_connections(jumperless, connections, *, skip_prompts, manual):
    """Program (and on exit clear) a set of Jumperless connections. Shared by the
    `wire` (marker-driven) and `digital_loopback` (descriptor-driven) fixtures.

    When no usable Jumperless was opened: --skip-wiring-prompts proceeds (pre-wired
    bench), --jumperless-manual prompts to wire by hand, otherwise the wired test is
    skipped (no board attached) rather than blocking on input(). This is the
    authoritative wired-skip — it keys off the fixture's real openability, so it also
    catches the found-but-unopenable case the collection-time heuristic can't."""
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
    elif manual:
        for label, (n1, n2) in connections.items():
            input(f"  [{label}]  connect {n1} → {n2}, then press Enter ... ")
        try:
            yield
        finally:
            input("  Test done — remove connections, press Enter ... ")
    else:
        pytest.skip(
            "no Jumperless attached; wired test skipped "
            "(--jumperless-manual to wire by hand, --skip-wiring-prompts if pre-wired)"
        )


# Per-device digital loopback: output pin -> input pin, with the device's GND reference.
# Keys are devid. sig_* are pinout.py signal names; out/inp are device pin names.
_DIGITAL_LOOPBACK = {
    10: dict(out="dio0",  inp="dio1",  sig_out="DIO0",    sig_in="DIO1",    gnd="AD3_GND"),
    4:  dict(out="dio24", inp="dio25", sig_out="DIO24",   sig_in="DIO25",   gnd="DD_GND"),
    # ADP2230 (devid 14): wired face-in on the right of the board; DIO0/DIO1/GND rows
    # measured at the bench (pinout.py ADP_*_ROW, env-overridable).
    14: dict(out="dio0",  inp="dio1",  sig_out="ADP_DIO0", sig_in="ADP_DIO1", gnd="ADP_GND"),
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
                           skip_prompts=pytestconfig.getoption("--skip-wiring-prompts"),
                           manual=pytestconfig.getoption("--jumperless-manual")):
        yield (spec["out"], spec["inp"])


def _parse_cabled_channels() -> set[int]:
    """Scope channels declared as physically cabled (W1->CHn) via ADP_AWG_SCOPE_CHANNELS
    (e.g. "1", "2", "1,2"). Used for analog loopback on devices whose analog I/O the
    Jumperless can't route (the ADP2230's BNC connectors)."""
    out: set[int] = set()
    for tok in os.environ.get("ADP_AWG_SCOPE_CHANNELS", "").replace(",", " ").split():
        with contextlib.suppress(ValueError):
            out.add(int(tok))
    return out


# Per-device AWG W1 -> scope CHn loopback descriptor. `jumperless` holds the Jumperless
# wiring when the device's analog I/O is on header rows it can reach (AD3); None means
# the analog I/O is on BNC connectors the Jumperless can't wire (ADP2230) -> manual cable,
# and the scope channel is taken from ADP_AWG_SCOPE_CHANNELS (only W1 is an AWG output).
_ANALOG_LOOPBACK: dict[int, dict] = {
    10: dict(  # AD3: W1 and CH1 are on the flywire header -> Jumperless-routable.
        awg_channel=1,
        scope_channel=1,
        jumperless={
            "gnd_bridge": ("AD3_GND", "GND"),
            "ch1_neg": ("CH1_NEG", "AD3_GND"),
            "awg_to_scope": ("W1", "CH1_POS"),
        },
    ),
    14: dict(awg_channel=1, scope_channel=None, jumperless=None),  # ADP2230: BNC cable.
}


@pytest.fixture
def analog_loopback(request, dut_caps, jumperless, pytestconfig, _require):
    """Yield (awg_channel, scope_channel) for a W1 -> scope loopback, wired for the
    connected device. On the AD3 the loopback is auto-routed through the Jumperless
    (autonomous). On the ADP2230 the analog I/O is on BNC connectors the Jumperless
    can't reach, so it needs a manual cable from W1 to a scope channel declared via
    ADP_AWG_SCOPE_CHANNELS (skipped otherwise). The AWG channel is always 1 (W1)."""
    if request.node.get_closest_marker("jumperless") is not None:
        raise RuntimeError("analog_loopback tests must not also use @pytest.mark.jumperless")
    if dut_caps is None:
        pytest.skip("no DUT available")
    spec = _ANALOG_LOOPBACK.get(dut_caps.devid)
    if spec is None:
        pytest.skip(f"no analog-loopback descriptor for devid {dut_caps.devid}")
    if spec["jumperless"] is not None:
        with route_connections(jumperless, spec["jumperless"],
                               skip_prompts=pytestconfig.getoption("--skip-wiring-prompts"),
                               manual=pytestconfig.getoption("--jumperless-manual")):
            yield (spec["awg_channel"], spec["scope_channel"])
    else:
        cabled = _parse_cabled_channels()
        if not cabled:
            pytest.skip(
                f"devid {dut_caps.devid} analog I/O is on BNC (not Jumperless-routable); "
                "set ADP_AWG_SCOPE_CHANNELS=<scope ch> with W1 cabled to that CH"
            )
        # AWG output is always W1 (channel 1); the scope channel is whichever the
        # operator cabled W1 into (lowest if several declared).
        yield (spec["awg_channel"], sorted(cabled)[0])


@pytest.fixture(autouse=True)
def wire(request: pytest.FixtureRequest, jumperless, pytestconfig: pytest.Config, _require):
    marker = request.node.get_closest_marker("jumperless")
    if marker is None:
        yield
        return

    connections = marker.kwargs.get("connections", {})
    with route_connections(jumperless, connections,
                           skip_prompts=pytestconfig.getoption("--skip-wiring-prompts"),
                           manual=pytestconfig.getoption("--jumperless-manual")):
        yield
