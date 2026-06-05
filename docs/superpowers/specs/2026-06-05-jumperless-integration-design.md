# Jumperless V5 Hardware Test Automation — Design Spec

## Goal

Make hardware tests in `tests/hardware/` self-wiring when a Jumperless V5 is present,
while falling back gracefully to interactive manual prompts (or silent skip) when it is not.
No test logic changes; wiring is fully orthogonal to test assertions.

---

## Background

The Jumperless V5 is a programmable breadboard with a MicroPython Raw REPL. Its Python
driver (`jumperless-py`) exposes `Jumperless.connect(node1, node2)` and `nodes_clear()`.
Node IDs are integers 1–60 (breadboard rows) or string aliases (`"GND"`, `"TOP_RAIL"`, etc.).

The AD3 (Analog Discovery 3) is seated in the breadboard; its 2×15 header occupies two
rows — one per connector side. Because the connector is inserted with left/right flipped
relative to the pinout diagram, pin offsets within each side are reversed.

---

## Detection hierarchy

Checked in this order — earlier conditions short-circuit the rest:

1. `--skip-wiring-prompts` CLI flag set → skip all prompts, no device import or probe (CI / pre-wired bench)
2. `jumperless-py` not installed → manual prompts
3. Installed, but `find_jumperless_ports()` returns fewer than 3 ports → manual prompts
   _(The Jumperless V5 exposes exactly 3 USB serial ports: main terminal, Arduino passthrough,
   and MicroPython Raw REPL. Fewer than 3 means the device is absent or not fully enumerated.)_
4. Installed, device found, `--jumperless-manual` CLI flag set → manual prompts
5. Installed, device found, no override → auto-wire

`jumperless-py` is an optional dependency. It is never imported at module level in the
production `dwf_mcp` package — only inside the test fixtures.

---

## New files

| Path | Purpose |
|------|---------|
| `tests/hardware/pinout.py` | AD3 signal name → Jumperless row number |
| `tests/hardware/conftest.py` | `jumperless` session fixture, `wire` autouse fixture, `app` function fixture |

## Modified files

| Path | Change |
|------|--------|
| `tests/conftest.py` | Add CLI options and marker registration |
| `tests/hardware/test_*.py` (all 12) | Add `@pytest.mark.jumperless` marker only — no signature change |

---

## Pinout module (`tests/hardware/pinout.py`)

### Layout constants

```python
N_PER_SIDE = 15

AD3_TOP_ROW = int(os.environ.get("AD3_TOP_ROW", "1"))   # odd-side connector base row
AD3_BOT_ROW = int(os.environ.get("AD3_BOT_ROW", "16"))  # even-side connector base row
```

### Signal map

`_SIGNAL_MAP` maps every AD3 signal name to `("top"|"bot", offset)` where offset 0 is
pin 1 of that connector side per the AD3 datasheet. Jumperless built-in node aliases
(`"GND"`, `"TOP_RAIL"`, `"BOTTOM_RAIL"`, `"DAC0"`, `"DAC1"`, `"ADC0"`–`"ADC4"`) map
directly to their string values.

Signals covered: `W1`, `W2`, `CH1_POS`, `CH1_NEG`, `CH2_POS`, `CH2_NEG`,
`DIO0`–`DIO15`, `TRIG_IN`, `TRIG_OUT`, `GND`, `VCC` (→ `"TOP_RAIL"`).

### `row()` function

```python
def row(signal: str) -> int | str:
    entry = _SIGNAL_MAP[signal]
    if isinstance(entry, str):          # Jumperless built-in alias ("GND", "TOP_RAIL", …)
        return entry
    if isinstance(entry, int):          # pre-placed component node — direct row number
        return entry
    side, offset = entry
    base = AD3_TOP_ROW if side == "top" else AD3_BOT_ROW
    return base + (N_PER_SIDE - 1 - offset)   # reversed: connector is physically flipped
```

Changing `AD3_TOP_ROW` / `AD3_BOT_ROW` env vars relocates all AD3 pins without touching
any test file.

### Pre-placed component nodes

Components placed **vertically** (bridging the DIP center gap) get two named entries — one
per leg. Neither leg is pre-committed to a rail; the `wire` fixture connects both dynamically.

```python
_SIGNAL_MAP = {
    # … AD3 signals …
    # Pre-placed passives — vertical, bridging DIP center gap
    # (update row numbers to match physical placement)
    "I2C_SDA_R_A": 28,   # R1 top-half leg
    "I2C_SDA_R_B": 58,   # R1 bottom-half leg (same column, other side of gap)
    "I2C_SCL_R_A": 29,   # R2 top-half leg
    "I2C_SCL_R_B": 59,   # R2 bottom-half leg
}
```

Pre-placed component names go in `_SIGNAL_MAP` as plain integers. `row()` returns them directly (the `isinstance(entry, int)` branch).

---

## CLI options and marker (`tests/conftest.py`)

```python
def pytest_addoption(parser):
    parser.addoption("--jumperless-manual", action="store_true",
                     help="Force manual wiring prompts even if Jumperless device is found")
    parser.addoption("--skip-wiring-prompts", action="store_true",
                     help="Skip all wiring prompts — for CI or pre-wired bench")

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "jumperless(connections): dict of label -> (signal1, signal2) connections required",
    )
```

---

## Hardware conftest (`tests/hardware/conftest.py`)

### `jumperless` fixture (session-scoped)

Opens the device once at session start, closes at session end. Returns `None` when
auto-wiring is unavailable (the `wire` fixture handles the fallback).
`--skip-wiring-prompts` is checked first so CI never imports or probes the serial bus.
Probe and open failures (permission errors, stale ports, busy Raw REPL) are caught and
treated as "no device" with a warning rather than failing fixture setup.

```python
@pytest.fixture(scope="session")
def jumperless(pytestconfig):
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
        warnings.warn(f"Jumperless probe/open failed ({exc!r}), falling back to manual prompts")
        yield None
        return
    try:
        yield j
    finally:
        j.close()
```

### `app` fixture (function-scoped)

Used only by the four server-style tests (`dmm`, `uart`, `spi`, `can`) that talk to the
device through the MCP `build_app` interface. Function-scoped so each test gets a fresh
device open/close — this prevents cross-test state leakage and avoids conflicting with
the low-level tests (eight remaining tests) which open their own `PydwfBackend` directly.
A session-scoped shared app would cause a double-open conflict with those tests.

```python
@pytest.fixture
def app():
    a = build_app(backend_name="pydwf")
    asyncio.run(a.call_tool("waveforms.open", {}))
    try:
        yield a
    finally:
        asyncio.run(a.call_tool("waveforms.close", {}))
```

### `wire` fixture (autouse, function-scoped)

Autouse: runs for every hardware test. Short-circuits immediately when the test has no
`jumperless` marker, so non-marked tests are unaffected.

If the device is available, clears the board, connects all listed signal pairs, and clears
again in `finally` (cleanup runs even if the test errors). If unavailable and prompts are
not suppressed, prints interactive prompts — teardown prompt also in `finally`.

`nodes_clear()` is safe here because the invariant is: **all non-test Jumperless state is
physical only** (components on breadboard rows, rails via header pins). There is no
persistent programmatic routing outside of what `wire` itself adds.

```python
@pytest.fixture(autouse=True)
def wire(request, jumperless, pytestconfig):
    marker = request.node.get_closest_marker("jumperless")
    if marker is None:
        yield
        return

    connections = marker.kwargs.get("connections", {})
    skip = pytestconfig.getoption("--skip-wiring-prompts")

    if jumperless is not None:
        jumperless.nodes_clear()   # start clean
        for n1, n2 in connections.values():
            jumperless.connect(pinout.row(n1), pinout.row(n2))
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
```

---

## Retrofit pattern

Adding Jumperless support to an existing test requires one line — add the marker.
No signature change needed: `wire` is autouse.

```python
# before
@pytest.mark.hardware
def test_dio_loopback_high_low(tmp_path: Path) -> None:

# after
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_dio_loopback_high_low(tmp_path: Path) -> None:
```

Test body is unchanged. Tests without the marker are unaffected — `wire` short-circuits
when no marker is present.

The four server-style tests (`dmm`, `uart`, `spi`, `can`) currently call `build_app`
internally. As part of the retrofit, replace those inline `build_app` + `waveforms.open`
calls with the `app` fixture so teardown is guaranteed even on failure:

```python
# before (dmm / uart / spi / can pattern)
@pytest.mark.hardware
def test_uart_loopback() -> None:
    app = build_app(backend_name="pydwf")
    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        ...
        await app.call_tool("waveforms.close", {})
    asyncio.run(run())

# after
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_uart_loopback(app) -> None:
    async def run() -> None:
        ...  # body unchanged, waveforms.open/close removed
    asyncio.run(run())
```

---

## Connection conventions per instrument

Derived from the wiring comments at the top of each existing test file:

| Test file | Connections |
|-----------|------------|
| `test_spi_hardware.py` | `{"loopback": ("DIO1", "DIO2")}` — MOSI(DIO1) → MISO(DIO2) |
| `test_uart_hardware.py` | `{"loopback": ("DIO0", "DIO1")}` — TX → RX |
| `test_can_hardware.py` | `{"loopback": ("DIO0", "DIO1")}` — TX → RX |
| `test_i2c_hardware.py` | `{"sda_pwr": ("TOP_RAIL","I2C_SDA_R_A"), "sda_sig": ("DIO0","I2C_SDA_R_B"), "scl_pwr": ("TOP_RAIL","I2C_SCL_R_A"), "scl_sig": ("DIO1","I2C_SCL_R_B")}` |
| `test_awg_hardware.py` | `{"awg_to_scope": ("W1", "CH1_POS")}` |
| `test_scope_hardware.py` | `{"awg_to_scope": ("W1", "CH1_POS")}` |
| `test_scope_record_hardware.py` | `{"ch1": ("W1", "CH1_POS"), "ch2": ("W2", "CH2_POS")}` |
| `test_dmm_hardware.py` | `{"awg_to_scope": ("W1", "CH1_POS")}` |
| `test_logic_hardware.py` | `{"loopback": ("DIO0", "DIO1")}` — pattern → logic |
| `test_dio_hardware.py` | `{"loopback": ("DIO0", "DIO1")}` — out → in |
| `test_supply_hardware.py` | _(no wiring required — tests V+ rail directly)_ |
| `test_pydwf_backend.py` | _(no wiring required — enumerate/open only)_ |

---

## Physical setup requirements

- AD3 header seated in breadboard rows `AD3_TOP_ROW` through `AD3_TOP_ROW + 14` (top side)
  and `AD3_BOT_ROW` through `AD3_BOT_ROW + 14` (bottom side).
- Pull-up resistors and other passives placed **vertically**, bridging the DIP center gap,
  at the rows named in `pinout.py`. Neither leg pre-wired.
- No other permanent wiring required — the Jumperless routes everything else.

---

## Out of scope

- Multi-DUT testing (single AD3 + single Jumperless only)
- Jumperless DAC/ADC use for stimulus/sense (AD3 handles all measurement)
- Changes to `dwf_mcp` production source — this is test infrastructure only
