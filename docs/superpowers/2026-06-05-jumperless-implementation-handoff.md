# Jumperless Integration — Implementation Handoff

## What you are building

Test infrastructure that automatically wires the AD3 hardware for each test when a
Jumperless V5 programmable breadboard is present. When the Jumperless is absent, tests
fall back to interactive manual prompts. No test assertion code changes.

**Full spec:** `docs/superpowers/specs/2026-06-05-jumperless-integration-design.md`
Read it completely before starting. This document fills in context the spec omits.

---

## Repository layout you need to know

```
dwf-mcp/
  src/dwf_mcp/
    server.py            # build_app() — used by server-style tests
    backends/
      pydwf_backend.py   # real AD3 hardware backend
  tests/
    conftest.py          # DOES NOT EXIST YET — you create it
    hardware/
      conftest.py        # DOES NOT EXIST YET — you create it
      pinout.py          # DOES NOT EXIST YET — you create it
      test_awg_hardware.py
      test_can_hardware.py
      test_dio_hardware.py
      test_dmm_hardware.py
      test_i2c_hardware.py
      test_logic_hardware.py
      test_pydwf_backend.py
      test_scope_hardware.py
      test_scope_record_hardware.py
      test_spi_hardware.py
      test_supply_hardware.py
      test_uart_hardware.py
```

---

## The two existing test styles

The 12 hardware tests fall into two camps. Each needs different retrofit treatment.

### Low-level style (8 tests)

Create `PydwfBackend` + `DwfDevice` directly inside the test function. Open and close
in a try/finally. Example from `test_awg_hardware.py`:

```python
@pytest.mark.hardware
def test_awg_sine_captured_by_scope(tmp_path: Path) -> None:
    pytest.importorskip("pydwf")
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    ...
    device = DwfDevice(backend=PydwfBackend(), ...)
    device.open()
    try:
        ...
    finally:
        device.close()
```

**Retrofit:** Add `@pytest.mark.jumperless(...)` marker only. No signature change.
Body unchanged. The `wire` autouse fixture handles wiring without touching the test.

Low-level style: `test_awg`, `test_dio`, `test_i2c`, `test_logic`, `test_pydwf_backend`,
`test_scope`, `test_scope_record`, `test_supply`.

### Server style (4 tests)

Use `build_app(backend_name="pydwf")` and call tools via `app.call_tool(...)`.
Currently they call `waveforms.open` / `waveforms.close` inside the test. Example
from `test_uart_hardware.py`:

```python
@pytest.mark.hardware
def test_uart_loopback() -> None:
    app = build_app(backend_name="pydwf")
    async def run() -> None:
        await app.call_tool("waveforms.open", {})
        await app.call_tool("uart.configure", {...})
        ...
        await app.call_tool("waveforms.close", {})
    asyncio.run(run())
```

**Retrofit:** Add `@pytest.mark.jumperless(...)` marker AND add `app` to the signature
(replacing the inline `build_app` + `waveforms.open/close`). The `app` fixture is
function-scoped and handles open/close with try/finally. Remove the inline open/close
calls — the fixture owns them now.

After retrofit:

```python
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_uart_loopback(app) -> None:
    async def run() -> None:
        await app.call_tool("uart.configure", {"baud_rate": 9600, ...})
        ...  # everything else identical, open/close removed
    asyncio.run(run())
```

Server style: `test_can`, `test_dmm`, `test_spi`, `test_uart`.

---

## Connections for each test

Derived from the wiring comments at the top of each test file. These are exactly what
goes in each `@pytest.mark.jumperless(connections={...})` call:

| Test | connections dict |
|------|-----------------|
| `test_awg` | `{"awg_to_scope": ("W1", "CH1_POS")}` |
| `test_can` | `{"loopback": ("DIO0", "DIO1")}` |
| `test_dio` | `{"loopback": ("DIO0", "DIO1")}` |
| `test_dmm` | `{"awg_to_scope": ("W1", "CH1_POS")}` |
| `test_i2c` | `{"sda_pwr": ("TOP_RAIL","I2C_SDA_R_A"), "sda_sig": ("DIO0","I2C_SDA_R_B"), "scl_pwr": ("TOP_RAIL","I2C_SCL_R_A"), "scl_sig": ("DIO1","I2C_SCL_R_B")}` |
| `test_logic` | `{"loopback": ("DIO0", "DIO1")}` |
| `test_pydwf_backend` | _(no wiring — no marker needed)_ |
| `test_scope` | `{"awg_to_scope": ("W1", "CH1_POS")}` |
| `test_scope_record` | `{"ch1": ("W1", "CH1_POS"), "ch2": ("W2", "CH2_POS")}` |
| `test_spi` | `{"loopback": ("DIO1", "DIO2")}` — MOSI→MISO |
| `test_supply` | _(no wiring — no marker needed)_ |
| `test_uart` | `{"loopback": ("DIO0", "DIO1")}` |

---

## The AD3 connector pinout

The AD3 has a 2×15 (30-pin) header. You must look up the exact pinout from the official
Digilent Analog Discovery 3 reference manual to build `_SIGNAL_MAP` in `pinout.py`.

**Where to find it:**
- Digilent AD3 Resource Center: https://digilent.com/reference/test-and-measurement/analog-discovery-3/start
- Look for "Pinout" or "Connector" section in the reference manual

The signals you need to map (all of them):
- `W1`, `W2` — AWG outputs
- `CH1_POS`, `CH1_NEG`, `CH2_POS`, `CH2_NEG` — scope inputs
- `DIO0`–`DIO15` — digital I/O
- `TRIG_IN`, `TRIG_OUT` — trigger signals
- `GND` → maps to string `"GND"` (Jumperless built-in)
- `VCC` → maps to string `"TOP_RAIL"` (Jumperless built-in)

For each signal, determine which connector side (`"top"` or `"bot"`) and which offset
(0 = pin 1 of that side per the datasheet). The `row()` function reverses the offset
within each side because the AD3 is physically inserted with left/right flipped relative
to the datasheet diagram.

Structure:
```python
_SIGNAL_MAP: dict[str, tuple[str, int] | str | int] = {
    "W1":      ("top", 0),   # example — verify against datasheet
    "W2":      ("top", 1),   # example — verify against datasheet
    # ... all 30 signals ...
    "GND":     "GND",
    "VCC":     "TOP_RAIL",
    # Pre-placed I2C pull-up resistors (physical row numbers, set at placement time)
    "I2C_SDA_R_A": 28,
    "I2C_SDA_R_B": 58,
    "I2C_SCL_R_A": 29,
    "I2C_SCL_R_B": 59,
}
```

---

## Key packages and imports

```python
# jumperless-py (optional; guarded by try/except ImportError)
from jumperless import Jumperless, find_jumperless_ports

# find_jumperless_ports() -> list[str]  (sorted device paths)
# len < 3 means device absent or not fully enumerated
# Jumperless V5 always exposes exactly 3 USB serial ports

# Jumperless API used in wire fixture
j = Jumperless()           # auto-detects 3rd port (MicroPython Raw REPL)
j.connect(node1, node2)    # both args: int row (1-60) or str alias
j.nodes_clear()            # remove all programmatic connections
j.close()                  # close serial port

# node aliases accepted as strings: "GND", "TOP_RAIL", "BOTTOM_RAIL",
# "DAC0", "DAC1", "ADC0"–"ADC4"
```

---

## Fixture dependency map

```
tests/conftest.py
  pytest_addoption          → adds --jumperless-manual, --skip-wiring-prompts
  pytest_configure          → registers "jumperless" marker

tests/hardware/conftest.py
  jumperless   (session)    → Jumperless | None
  app          (function)   → opened build_app instance
  wire         (autouse)    → reads marker, connects/prompts, always clears in finally
```

`wire` depends on `jumperless` and `pytestconfig`. It does NOT depend on `app` —
they are independent. A low-level test gets `wire` (autouse) but never gets `app`.
A server-style test gets both `wire` (autouse) and `app` (explicit in signature).

---

## What to test (fixture unit tests)

Write `tests/unit/test_jumperless_fixtures.py`. No hardware required — mock or monkeypatch
the serial probe. Cover:

1. `--skip-wiring-prompts` → `jumperless` yields None without importing or probing
2. `ImportError` on `from jumperless import ...` → yields None
3. `find_jumperless_ports()` returns `< 3` ports → yields None
4. `--jumperless-manual` → yields None even when 3 ports found
5. `find_jumperless_ports()` raises `Exception` → yields None + emits warning
6. `Jumperless()` raises `Exception` → yields None + emits warning
7. `wire` with no marker → neither `connect` nor `nodes_clear` called
8. `wire` with marker + real jumperless → `nodes_clear` before, `connect` per pair, `nodes_clear` after
9. `wire` with marker + test failure → `nodes_clear` still called (finally)
10. `wire` with marker + no jumperless + skip → yields without prompting
11. `row("W1")` → correct integer given default AD3_TOP_ROW / AD3_BOT_ROW
12. `row("GND")` → `"GND"` (string passthrough)
13. `row("I2C_SDA_R_A")` → 28 (integer passthrough)
14. `row("UNKNOWN")` → `KeyError`

---

## Conventions in this codebase

- No docstrings; inline comments only for non-obvious WHY
- `from __future__ import annotations` at top of every file
- Type hints throughout; `mypy src/` must pass (tests are not type-checked by default)
- `ruff check src/ tests/` must pass
- Hardware tests use `pytest.importorskip("pydwf")` at test start (low-level style) or
  rely on `build_app(backend_name="pydwf")` failing gracefully (server style)
- All hardware tests decorated with `@pytest.mark.hardware`
- Commit after each logical unit of work

---

## Run commands

```bash
cd ~/work/dwf-mcp/dwf-mcp

# Unit tests (no hardware required)
pytest tests/unit/ -v

# Hardware tests (AD3 + Jumperless connected)
pytest -m hardware -v

# Hardware tests, manual wiring prompts
pytest -m hardware -v --jumperless-manual

# CI / no prompts
pytest -m hardware -v --skip-wiring-prompts
```

---

## Implementation order (suggested)

1. `tests/conftest.py` — CLI options + marker registration + unit tests for the hook
2. `tests/hardware/pinout.py` — signal map (look up AD3 datasheet first) + unit tests
3. `tests/hardware/conftest.py` — all three fixtures + unit tests for fixture behaviour
4. Retrofit 10 marked tests (add markers, convert 4 server-style to use `app` fixture)
5. Verify `pytest tests/unit/ -v` passes (no hardware needed)
