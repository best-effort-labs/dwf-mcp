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
| `tests/hardware/conftest.py` | `jumperless` session fixture, `wire` function fixture, `app` session fixture |

## Modified files

| Path | Change |
|------|--------|
| `tests/conftest.py` | Add CLI options and marker registration |
| `tests/hardware/test_*.py` (all 12) | Add `@pytest.mark.jumperless` marker + `wire` fixture |

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

def pytest_runtest_setup(item):
    """Warn at collection time if jumperless marker is present but wire fixture is missing."""
    if item.get_closest_marker("jumperless") and "wire" not in item.fixturenames:
        warnings.warn(
            f"{item.nodeid}: has @pytest.mark.jumperless but does not request the 'wire' fixture "
            "— connections will not be made",
            stacklevel=2,
        )
```

---

## Hardware conftest (`tests/hardware/conftest.py`)

### `jumperless` fixture (session-scoped)

Opens the device once at session start, closes at session end. Returns `None` when
auto-wiring is unavailable (the `wire` fixture handles the fallback).
`--skip-wiring-prompts` is checked first so CI never imports or probes the serial bus.

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
    if (len(find_jumperless_ports()) < 3
            or pytestconfig.getoption("--jumperless-manual")):
        yield None
        return
    j = Jumperless()
    try:
        yield j
    finally:
        j.close()
```

### `app` fixture (session-scoped)

`waveforms.open` initializes the device but does not configure any channels or require
specific wiring to be in place. Wiring is set up by `wire` (function-scoped) before the
test body runs — the session/function scope difference is safe here.

```python
@pytest.fixture(scope="session")
def app():
    a = build_app(backend_name="pydwf")
    asyncio.run(a.call_tool("waveforms.open", {}))
    yield a
    asyncio.run(a.call_tool("waveforms.close", {}))
```

### `wire` fixture (function-scoped)

Reads the `jumperless` marker from the test node. If the device is available, clears the
board, connects all listed signal pairs, and clears again in `finally` (so cleanup runs
even if the test errors). If unavailable and prompts are not suppressed, prints interactive
prompts — teardown prompt also runs in `finally`.

`nodes_clear()` is safe here because the invariant is: **all non-test Jumperless state is
physical only** (components on breadboard rows, rails via header pins). There is no
persistent programmatic routing outside of what `wire` itself adds.

```python
@pytest.fixture
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

Adding Jumperless support to an existing test requires two lines:

```python
# before
@pytest.mark.hardware
def test_spi_loopback(app) -> None:

# after
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO1", "DIO2")})
def test_spi_loopback(app, wire) -> None:
```

Test body is unchanged. Tests without `wire` in their signature ignore the marker entirely
and behave as before — no regressions for tests that don't opt in.

---

## Connection conventions per instrument

| Test | Connections |
|------|------------|
| SPI loopback | `("DIO1", "DIO2")` — MOSI → MISO |
| UART loopback | `("DIO0", "DIO1")` — TX → RX |
| I2C | `("TOP_RAIL", "I2C_SDA_R_A")`, `("DIO4", "I2C_SDA_R_B")`, `("TOP_RAIL", "I2C_SCL_R_A")`, `("DIO5", "I2C_SCL_R_B")` |
| Scope / AWG | `("W1", "CH1_POS")` and/or `("W2", "CH2_POS")` |
| Logic / Pattern | `("DIO0", "DIO4")` etc. per test |
| DMM | `("W1", "CH1_POS")` for voltage; `("DAC0", "CH1_POS")` for current path |

Exact DIO indices per instrument match the `configure` call in the test.

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
