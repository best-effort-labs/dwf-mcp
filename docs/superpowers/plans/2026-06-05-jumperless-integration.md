# Jumperless Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add self-wiring test infrastructure so hardware tests automatically route the AD3 via Jumperless V5 when present, falling back to interactive prompts when not.

**Architecture:** Three new files (`tests/conftest.py`, `tests/hardware/pinout.py`, `tests/hardware/conftest.py`) plus a unit test file carry all new logic. Ten existing hardware test files get a single `@pytest.mark.jumperless(...)` marker added; four server-style tests additionally swap their inline `build_app`/open/close for the new `app` fixture. No production source changes.

**Tech Stack:** Python 3.11+, pytest ≥ 7, `jumperless-py` (optional at runtime), `unittest.mock`, `inspect.unwrap` for fixture unit-testing.

> **Orientation note:** By default (`AD3_FLIP=1`) the plan assumes the AD3 is inserted with pins physically reversed relative to the Digilent datasheet diagram — the common breadboard orientation. Set `AD3_FLIP=0` if the AD3 is plugged in face-first via a ribbon cable or adapter, which aligns physical pin order with the datasheet and skips the reversal in `row()`.

---

## AD3 Pinout Reference

Derived from the physical device labels (left→right as printed on green case). The device is inserted left/right-flipped relative to the Digilent datasheet, so **datasheet offset = 14 − physical_position** within each row. The `row()` function applies this reversal automatically.

```
Physical (left→right):  1+  2+  ↓  V+  W1  ↓  T1  0  1  2  3  4  5   6   7
Datasheet offset:        14  13  12  11  10  9   8  7  6  5  4  3  2   1   0

Physical (left→right):  1-  2-  ↓  V-  W2  ↓  T2  8  9  10 11 12 13  14  15
Datasheet offset:        14  13  12  11  10  9   8  7  6   5  4  3  2   1   0
```

With defaults AD3_TOP_ROW=1, AD3_BOT_ROW=16, N_PER_SIDE=15, the key row() values are:

| Signal | row() |   | Signal | row() |
|--------|-------|---|--------|-------|
| CH1_POS | 1 | | CH1_NEG | 16 |
| CH2_POS | 2 | | CH2_NEG | 17 |
| TRIG_IN | 7 | | TRIG_OUT | 22 |
| W1      | 5 | | W2       | 20 |
| DIO0    | 8 | | DIO8     | 23 |
| DIO1    | 9 | | DIO9     | 24 |
| DIO2    | 10 | | DIO10   | 25 |
| DIO3    | 11 | | DIO11   | 26 |
| DIO4    | 12 | | DIO12   | 27 |
| DIO5    | 13 | | DIO13   | 28 |
| DIO6    | 14 | | DIO14   | 29 |
| DIO7    | 15 | | DIO15   | 30 |

---

## File Map

| Action | Path |
|--------|------|
| **Create** | `tests/conftest.py` |
| **Create** | `tests/hardware/pinout.py` |
| **Create** | `tests/hardware/conftest.py` |
| **Create** | `tests/unit/test_jumperless_fixtures.py` |
| **Modify** | `tests/hardware/test_awg_hardware.py` |
| **Modify** | `tests/hardware/test_can_hardware.py` |
| **Modify** | `tests/hardware/test_dio_hardware.py` |
| **Modify** | `tests/hardware/test_dmm_hardware.py` |
| **Modify** | `tests/hardware/test_i2c_hardware.py` |
| **Modify** | `tests/hardware/test_logic_hardware.py` |
| **Modify** | `tests/hardware/test_scope_hardware.py` |
| **Modify** | `tests/hardware/test_scope_record_hardware.py` |
| **Modify** | `tests/hardware/test_spi_hardware.py` |
| **Modify** | `tests/hardware/test_uart_hardware.py` |

---

## Task 1: `tests/conftest.py` — CLI options and marker registration

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Create the file**

```python
from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--jumperless-manual",
        action="store_true",
        help="Force manual wiring prompts even if Jumperless device is found",
    )
    parser.addoption(
        "--skip-wiring-prompts",
        action="store_true",
        help="Skip all wiring prompts — for CI or pre-wired bench",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "jumperless(connections): dict of label -> (signal1, signal2) connections required",
    )
```

- [ ] **Step 2: Run the unit suite to confirm no regressions**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/ -v
```

Expected: all existing unit tests still pass.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add jumperless CLI options and marker registration"
```

---

## Task 2: Pinout unit tests (write failing)

**Files:**
- Create: `tests/unit/test_jumperless_fixtures.py`

- [ ] **Step 1: Create the unit test file with row() tests**

```python
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(skip_wiring: bool = False, jumperless_manual: bool = False) -> MagicMock:
    cfg = MagicMock()
    def getoption(name: str, **kw: object) -> bool:
        if name in ("--skip-wiring-prompts",):
            return skip_wiring
        if name in ("--jumperless-manual",):
            return jumperless_manual
        return False
    cfg.getoption.side_effect = getoption
    return cfg


def _run_fixture(gen_func, *args):
    """Consume a fixture generator; return (yielded_value, did_cleanup)."""
    gen = gen_func(*args)
    val = next(gen)
    cleaned = False
    try:
        next(gen)
    except StopIteration:
        cleaned = True
    return val, cleaned


# ---------------------------------------------------------------------------
# row() tests (tests 11-14 from spec)
# ---------------------------------------------------------------------------

def test_row_w1_flipped_default():
    # AD3_FLIP=1 (default): W1 at ("top", 10), AD3_TOP_ROW=1 → 1 + (14-10) = 5
    import os
    import importlib
    env = {"AD3_FLIP": "1", "AD3_TOP_ROW": "1", "AD3_BOT_ROW": "16"}
    with patch.dict(os.environ, env):
        import tests.hardware.pinout as _p
        importlib.reload(_p)
        assert _p.row("W1") == 5


def test_row_w1_no_flip():
    # AD3_FLIP=0 (face-first via ribbon): W1 at ("top", 10), AD3_TOP_ROW=1 → 1 + 10 = 11
    import os
    import importlib
    env = {"AD3_FLIP": "0", "AD3_TOP_ROW": "1", "AD3_BOT_ROW": "16"}
    with patch.dict(os.environ, env):
        import tests.hardware.pinout as _p
        importlib.reload(_p)
        assert _p.row("W1") == 11


def test_row_gnd_string_passthrough():
    from tests.hardware.pinout import row
    assert row("GND") == "GND"


def test_row_i2c_resistor_integer_passthrough():
    from tests.hardware.pinout import row
    assert row("I2C_SDA_R_A") == 28


def test_row_unknown_raises_key_error():
    from tests.hardware.pinout import row
    with pytest.raises(KeyError):
        row("UNKNOWN_SIGNAL")
```

- [ ] **Step 2: Run to confirm the tests fail (pinout.py doesn't exist)**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/test_jumperless_fixtures.py -v
```

Expected: collection error or ERRORS — `ModuleNotFoundError: No module named 'tests.hardware.pinout'` (or similar import failure). No passes yet.

---

## Task 3: `tests/hardware/pinout.py` — signal map and `row()`

**Files:**
- Create: `tests/hardware/pinout.py`

- [ ] **Step 1: Create pinout.py**

```python
from __future__ import annotations

import os

N_PER_SIDE = 15

AD3_TOP_ROW = int(os.environ.get("AD3_TOP_ROW", "1"))
AD3_BOT_ROW = int(os.environ.get("AD3_BOT_ROW", "16"))
# AD3_FLIP=1 (default): AD3 inserted reversed relative to datasheet — row() reverses offset.
# AD3_FLIP=0: AD3 plugged face-first via ribbon/adapter — physical order matches datasheet.
AD3_FLIP = os.environ.get("AD3_FLIP", "1") == "1"

# Offsets are DATASHEET positions (0 = pin 1 per datasheet).
_SIGNAL_MAP: dict[str, tuple[str, int] | str | int] = {
    # Scope inputs — top row physical positions 0,1 → datasheet offsets 14,13
    "CH1_POS":  ("top", 14),
    "CH2_POS":  ("top", 13),
    # Scope inputs — bottom row
    "CH1_NEG":  ("bot", 14),
    "CH2_NEG":  ("bot", 13),
    # AWG outputs
    "W1":       ("top", 10),
    "W2":       ("bot", 10),
    # Triggers
    "TRIG_IN":  ("top", 8),
    "TRIG_OUT": ("bot", 8),
    # Power — map to Jumperless rail aliases instead of breadboard rows
    "VCC":      "TOP_RAIL",
    # Digital I/O, top row (physical positions 7-14 → offsets 7-0)
    "DIO0":     ("top", 7),
    "DIO1":     ("top", 6),
    "DIO2":     ("top", 5),
    "DIO3":     ("top", 4),
    "DIO4":     ("top", 3),
    "DIO5":     ("top", 2),
    "DIO6":     ("top", 1),
    "DIO7":     ("top", 0),
    # Digital I/O, bottom row (physical positions 7-14 → offsets 7-0)
    "DIO8":     ("bot", 7),
    "DIO9":     ("bot", 6),
    "DIO10":    ("bot", 5),
    "DIO11":    ("bot", 4),
    "DIO12":    ("bot", 3),
    "DIO13":    ("bot", 2),
    "DIO14":    ("bot", 1),
    "DIO15":    ("bot", 0),
    # Jumperless built-in node aliases — pass through as strings
    "GND":          "GND",
    "TOP_RAIL":     "TOP_RAIL",
    "BOTTOM_RAIL":  "BOTTOM_RAIL",
    "DAC0":         "DAC0",
    "DAC1":         "DAC1",
    "ADC0":         "ADC0",
    "ADC1":         "ADC1",
    "ADC2":         "ADC2",
    "ADC3":         "ADC3",
    "ADC4":         "ADC4",
    # Pre-placed I2C pull-up resistors — direct row numbers (vertical, bridging gap)
    "I2C_SDA_R_A":  28,
    "I2C_SDA_R_B":  58,
    "I2C_SCL_R_A":  29,
    "I2C_SCL_R_B":  59,
}


def row(signal: str) -> int | str:
    entry = _SIGNAL_MAP[signal]
    if isinstance(entry, str):
        return entry
    if isinstance(entry, int):
        return entry
    side, offset = entry
    base = AD3_TOP_ROW if side == "top" else AD3_BOT_ROW
    return base + (N_PER_SIDE - 1 - offset) if AD3_FLIP else base + offset
```

- [ ] **Step 2: Run pinout tests to verify they pass**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/test_jumperless_fixtures.py -v -k "row"
```

Expected: 5 tests pass — `test_row_w1_flipped_default`, `test_row_w1_no_flip`, `test_row_gnd_string_passthrough`, `test_row_i2c_resistor_integer_passthrough`, `test_row_unknown_raises_key_error`.

- [ ] **Step 3: Commit**

```bash
git add tests/hardware/pinout.py tests/unit/test_jumperless_fixtures.py
git commit -m "test: add pinout module with AD3 signal map and row() unit tests"
```

---

## Task 4: `jumperless` fixture unit tests (write failing)

**Files:**
- Modify: `tests/unit/test_jumperless_fixtures.py` — append tests 1–6

- [ ] **Step 1: Append jumperless fixture tests to the unit test file**

Add these functions after the existing `test_row_*` tests:

```python
# ---------------------------------------------------------------------------
# jumperless session fixture tests (tests 1-6 from spec)
# ---------------------------------------------------------------------------

def _run_jumperless(pytestconfig):
    """Helper: unwrap and run the jumperless session fixture."""
    from tests.hardware import conftest as hw
    gen_func = inspect.unwrap(hw.jumperless)
    return _run_fixture(gen_func, pytestconfig)


def test_jumperless_skip_flag_yields_none_without_probe():
    # --skip-wiring-prompts → None, no import or probe attempted
    cfg = _make_config(skip_wiring=True)
    with patch.dict("sys.modules", {"jumperless": None}):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_import_error_yields_none():
    cfg = _make_config()
    with patch("builtins.__import__", side_effect=ImportError):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_fewer_than_3_ports_yields_none():
    cfg = _make_config()
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_jumperless_ports.return_value = ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    with patch.dict("sys.modules", {"jumperless": mock_jl_mod}):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_manual_flag_yields_none_even_with_3_ports():
    cfg = _make_config(jumperless_manual=True)
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_jumperless_ports.return_value = ["/dev/cu.0", "/dev/cu.1", "/dev/cu.2"]
    with patch.dict("sys.modules", {"jumperless": mock_jl_mod}):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_probe_exception_yields_none_with_warning():
    cfg = _make_config()
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_jumperless_ports.side_effect = OSError("permission denied")
    with patch.dict("sys.modules", {"jumperless": mock_jl_mod}):
        with pytest.warns(UserWarning, match="Jumperless probe/open failed"):
            val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_open_exception_yields_none_with_warning():
    cfg = _make_config()
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_jumperless_ports.return_value = ["/dev/cu.0", "/dev/cu.1", "/dev/cu.2"]
    mock_jl_mod.Jumperless.side_effect = RuntimeError("stale REPL")
    with patch.dict("sys.modules", {"jumperless": mock_jl_mod}):
        with pytest.warns(UserWarning, match="Jumperless probe/open failed"):
            val, _ = _run_jumperless(cfg)
    assert val is None
```

- [ ] **Step 2: Run to confirm these tests fail**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/test_jumperless_fixtures.py -v -k "jumperless"
```

Expected: ERRORS — `ModuleNotFoundError` or `AttributeError` because `tests/hardware/conftest.py` doesn't exist yet.

---

## Task 5: `tests/hardware/conftest.py` — `jumperless` fixture

**Files:**
- Create: `tests/hardware/conftest.py`

- [ ] **Step 1: Create conftest.py with the jumperless fixture only**

```python
from __future__ import annotations

import asyncio
import warnings

import pytest

from dwf_mcp.server import build_app
from tests.hardware import pinout


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
```

- [ ] **Step 2: Run jumperless fixture tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/test_jumperless_fixtures.py -v -k "jumperless"
```

Expected: 6 tests pass. If `test_jumperless_import_error_yields_none` fails due to the `patch("builtins.__import__")` approach being too broad, use `patch.dict("sys.modules", {"jumperless": None})` pattern instead — see note below.

> **Note on mocking the import:** `patch.dict("sys.modules", {"jumperless": None})` causes `import jumperless` to raise `ImportError`. This is the correct mock for tests 2 and 3. For test 2 specifically, replace `patch("builtins.__import__", side_effect=ImportError)` with:
> ```python
> with patch.dict("sys.modules", {"jumperless": None}):
>     val, _ = _run_jumperless(cfg)
> ```
> If the jumperless module is already cached in `sys.modules` from a prior import, you may need to also pop it: `sys.modules.pop("jumperless", None)` before the patch.

- [ ] **Step 3: Run full unit suite to verify no regressions**

```bash
pytest tests/unit/ -v
```

Expected: all unit tests pass (19 now, including the new `test_row_w1_no_flip`).

- [ ] **Step 4: Commit**

```bash
git add tests/hardware/conftest.py tests/unit/test_jumperless_fixtures.py
git commit -m "test: add jumperless session fixture with unit tests"
```

---

## Task 6: `wire` fixture unit tests (write failing)

**Files:**
- Modify: `tests/unit/test_jumperless_fixtures.py` — append tests 7–10

- [ ] **Step 1: Append wire fixture tests**

Add after the jumperless fixture tests:

```python
# ---------------------------------------------------------------------------
# wire autouse fixture tests (tests 7-10 from spec)
# ---------------------------------------------------------------------------

def _make_marker(connections: dict) -> MagicMock:
    m = MagicMock()
    m.kwargs = {"connections": connections}
    return m


def _run_wire(request, jl, cfg):
    from tests.hardware import conftest as hw
    gen_func = inspect.unwrap(hw.wire)
    return gen_func(request, jl, cfg)


def test_wire_no_marker_skips_completely():
    request = MagicMock()
    request.node.get_closest_marker.return_value = None
    mock_jl = MagicMock()
    cfg = _make_config()
    gen = _run_wire(request, mock_jl, cfg)
    list(gen)
    mock_jl.connect.assert_not_called()
    mock_jl.nodes_clear.assert_not_called()


def test_wire_with_jumperless_connects_and_clears():
    from tests.hardware.pinout import row
    request = MagicMock()
    request.node.get_closest_marker.return_value = _make_marker(
        {"loopback": ("DIO0", "DIO1")}
    )
    mock_jl = MagicMock()
    cfg = _make_config()
    gen = _run_wire(request, mock_jl, cfg)
    next(gen)  # run up to yield (test body)
    assert mock_jl.nodes_clear.call_count == 1
    mock_jl.connect.assert_called_once_with(row("DIO0"), row("DIO1"))
    try:
        next(gen)  # trigger cleanup
    except StopIteration:
        pass
    assert mock_jl.nodes_clear.call_count == 2


def test_wire_clears_on_test_failure():
    request = MagicMock()
    request.node.get_closest_marker.return_value = _make_marker(
        {"loopback": ("DIO0", "DIO1")}
    )
    mock_jl = MagicMock()
    cfg = _make_config()
    gen = _run_wire(request, mock_jl, cfg)
    next(gen)
    try:
        gen.throw(RuntimeError("test failed"))
    except (RuntimeError, StopIteration):
        pass
    # finally block must still clear
    assert mock_jl.nodes_clear.call_count == 2


def test_wire_skip_prompts_no_input_called():
    request = MagicMock()
    request.node.get_closest_marker.return_value = _make_marker(
        {"loopback": ("DIO0", "DIO1")}
    )
    cfg = _make_config(skip_wiring=True)
    with patch("builtins.input") as mock_input:
        gen = _run_wire(request, None, cfg)  # jumperless=None
        next(gen)
        try:
            next(gen)
        except StopIteration:
            pass
    mock_input.assert_not_called()
```

- [ ] **Step 2: Run to confirm these 4 tests fail**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/test_jumperless_fixtures.py -v -k "wire"
```

Expected: ERRORS or FAIL — `AttributeError: module 'tests.hardware.conftest' has no attribute 'wire'`.

---

## Task 7: Add `wire` and `app` fixtures to `tests/hardware/conftest.py`

**Files:**
- Modify: `tests/hardware/conftest.py` — append two fixtures

- [ ] **Step 1: Append app and wire fixtures to conftest.py**

Add after the `jumperless` fixture:

```python
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

- [ ] **Step 2: Run wire fixture tests**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/test_jumperless_fixtures.py -v -k "wire"
```

Expected: 4 wire tests pass.

- [ ] **Step 3: Run the full unit suite**

```bash
pytest tests/unit/ -v
```

Expected: all 18 unit tests pass (14 new + existing).

- [ ] **Step 4: Commit**

```bash
git add tests/hardware/conftest.py tests/unit/test_jumperless_fixtures.py
git commit -m "test: add wire and app fixtures with unit tests"
```

---

## Task 8: Retrofit low-level tests — add `@pytest.mark.jumperless` markers

**Files:**
- Modify: `tests/hardware/test_awg_hardware.py`
- Modify: `tests/hardware/test_dio_hardware.py`
- Modify: `tests/hardware/test_i2c_hardware.py`
- Modify: `tests/hardware/test_logic_hardware.py`
- Modify: `tests/hardware/test_scope_hardware.py`
- Modify: `tests/hardware/test_scope_record_hardware.py`

No signature changes. The `wire` autouse fixture handles wiring. `test_pydwf_backend` and `test_supply` need no marker (no wiring required).

- [ ] **Step 1: Add marker to test_awg_hardware.py**

Change:
```python
@pytest.mark.hardware
def test_awg_sine_captured_by_scope(tmp_path: Path) -> None:
```
To:
```python
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"awg_to_scope": ("W1", "CH1_POS")})
def test_awg_sine_captured_by_scope(tmp_path: Path) -> None:
```

- [ ] **Step 2: Add marker to test_dio_hardware.py**

Change:
```python
@pytest.mark.hardware
def test_dio_loopback_high_low(tmp_path: Path) -> None:
```
To:
```python
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_dio_loopback_high_low(tmp_path: Path) -> None:
```

- [ ] **Step 3: Add marker to test_i2c_hardware.py**

Change:
```python
@pytest.mark.hardware
def test_i2c_scan_runs_without_error(tmp_path) -> None:
```
To:
```python
@pytest.mark.hardware
@pytest.mark.jumperless(connections={
    "sda_pwr": ("TOP_RAIL", "I2C_SDA_R_A"),
    "sda_sig": ("DIO0", "I2C_SDA_R_B"),
    "scl_pwr": ("TOP_RAIL", "I2C_SCL_R_A"),
    "scl_sig": ("DIO1", "I2C_SCL_R_B"),
})
def test_i2c_scan_runs_without_error(tmp_path) -> None:
```

- [ ] **Step 4: Add marker to test_logic_hardware.py**

Change:
```python
@pytest.mark.hardware
def test_pattern_clock_captured_by_logic(tmp_path: Path) -> None:
```
To:
```python
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_pattern_clock_captured_by_logic(tmp_path: Path) -> None:
```

- [ ] **Step 5: Add marker to test_scope_hardware.py**

Change:
```python
@pytest.mark.hardware
def test_scope_captures_1khz_sine_from_awg(tmp_path: Path) -> None:
```
To:
```python
@pytest.mark.hardware
@pytest.mark.jumperless(connections={"awg_to_scope": ("W1", "CH1_POS")})
def test_scope_captures_1khz_sine_from_awg(tmp_path: Path) -> None:
```

- [ ] **Step 6: Add marker to test_scope_record_hardware.py**

This file uses `pytestmark = pytest.mark.hardware` at the module level. Add module-level jumperless marks to each test function. The existing local `app` and `open_device` fixtures are module-scoped and must remain untouched.

Change:
```python
@pytest.mark.asyncio
async def test_scope_record_dc_signal(app, tmp_path: Path) -> None:
```
To:
```python
@pytest.mark.asyncio
@pytest.mark.jumperless(connections={"ch1": ("W1", "CH1_POS")})
async def test_scope_record_dc_signal(app, tmp_path: Path) -> None:
```

Change:
```python
@pytest.mark.asyncio
async def test_scope_record_two_channels(app, tmp_path: Path) -> None:
```
To:
```python
@pytest.mark.asyncio
@pytest.mark.jumperless(connections={"ch1": ("W1", "CH1_POS"), "ch2": ("W2", "CH2_POS")})
async def test_scope_record_two_channels(app, tmp_path: Path) -> None:
```

- [ ] **Step 7: Run unit tests to verify no regressions**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/ -v
```

Expected: all unit tests still pass (no hardware required).

- [ ] **Step 8: Commit**

```bash
git add tests/hardware/test_awg_hardware.py \
        tests/hardware/test_dio_hardware.py \
        tests/hardware/test_i2c_hardware.py \
        tests/hardware/test_logic_hardware.py \
        tests/hardware/test_scope_hardware.py \
        tests/hardware/test_scope_record_hardware.py
git commit -m "test: add jumperless markers to low-level hardware tests"
```

---

## Task 9: Retrofit server-style tests — markers + `app` fixture

**Files:**
- Modify: `tests/hardware/test_can_hardware.py`
- Modify: `tests/hardware/test_dmm_hardware.py`
- Modify: `tests/hardware/test_spi_hardware.py`
- Modify: `tests/hardware/test_uart_hardware.py`

For each: add the marker, add `app` to the signature, remove the inline `build_app` call, remove `waveforms.open` and `waveforms.close` (the `app` fixture owns open/close).

- [ ] **Step 1: Rewrite test_can_hardware.py**

```python
from __future__ import annotations

import asyncio
import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_can_send_receive_loopback(app) -> None:
    async def run() -> None:
        await app.call_tool("can.configure", {
            "tx_pin": "dio0", "rx_pin": "dio1", "bit_rate": 125_000,
        })
        result = await app.call_tool("can.receive", {"timeout_s": 1.0})
        await app.call_tool("can.send", {"id": 0x123, "data": [0x01, 0x02, 0x03]})
        result = await app.call_tool("can.receive", {"timeout_s": 1.0})
        assert result["id"] == 0x123, f"expected 0x123, got {result['id']}"
        assert result["data"] == [0x01, 0x02, 0x03]
        assert result["extended"] is False

    asyncio.run(run())
```

- [ ] **Step 2: Rewrite test_dmm_hardware.py**

```python
from __future__ import annotations

import asyncio
import time
import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"awg_to_scope": ("W1", "CH1_POS")})
def test_dmm_measures_awg_dc_voltage(app) -> None:
    async def run() -> None:
        await app.call_tool("awg.configure", {
            "channel": 1, "function": "DC",
            "frequency_hz": 1000.0, "amplitude_v": 2.0,
            "offset_v": 0.0, "phase_deg": 0.0, "symmetry": 50.0,
        })
        await app.call_tool("awg.start", {"channel": 1})
        time.sleep(0.05)
        result = await app.call_tool("dmm.measure", {"channel": 1, "range_v": 5.0})
        assert "mean_v" in result
        assert abs(result["mean_v"] - 2.0) < 0.1, f"expected ~2.0V, got {result['mean_v']}"
        await app.call_tool("awg.stop", {"channel": 1})

    asyncio.run(run())
```

- [ ] **Step 3: Rewrite test_spi_hardware.py**

```python
from __future__ import annotations

import asyncio
import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO1", "DIO2")})
def test_spi_loopback_transfer(app) -> None:
    async def run() -> None:
        await app.call_tool("spi.configure", {
            "clk_pin": "dio0", "frequency_hz": 1_000_000, "mode": 0,
            "mosi_pin": "dio1", "miso_pin": "dio2", "cs_pin": "dio3",
        })
        result = await app.call_tool("spi.transfer", {"data": [0xA5, 0x5A, 0xFF, 0x00]})
        assert result["sent"] == [0xA5, 0x5A, 0xFF, 0x00]
        assert result["received"] == result["sent"]

    asyncio.run(run())
```

- [ ] **Step 4: Rewrite test_uart_hardware.py**

```python
from __future__ import annotations

import asyncio
import time
import pytest


@pytest.mark.hardware
@pytest.mark.jumperless(connections={"loopback": ("DIO0", "DIO1")})
def test_uart_loopback(app) -> None:
    async def run() -> None:
        await app.call_tool("uart.configure", {
            "baud_rate": 9600, "tx_pin": "dio0", "rx_pin": "dio1",
        })
        await app.call_tool("uart.write", {"data": [0x48, 0x65, 0x6C, 0x6C, 0x6F]})
        time.sleep(0.05)
        result = await app.call_tool("uart.read", {"length": 5, "timeout_s": 1.0})
        assert result["data"] == [0x48, 0x65, 0x6C, 0x6C, 0x6F], f"got: {result['data']}"
        assert result["parity_error"] is False

    asyncio.run(run())
```

- [ ] **Step 5: Run unit tests to verify no regressions**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/ -v
```

Expected: all unit tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/hardware/test_can_hardware.py \
        tests/hardware/test_dmm_hardware.py \
        tests/hardware/test_spi_hardware.py \
        tests/hardware/test_uart_hardware.py
git commit -m "test: retrofit server-style hardware tests to use app fixture"
```

---

## Task 10: Final verification

- [ ] **Step 1: Run the full unit suite**

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest tests/unit/ -v
```

Expected: all 18 unit tests pass. Zero failures.

- [ ] **Step 2: Lint**

```bash
ruff check src/ tests/
```

Expected: no errors.

- [ ] **Step 3: Check skip-wiring-prompts works across the hardware suite**

```bash
pytest -m hardware -v --skip-wiring-prompts --collect-only
```

Expected: 12 hardware tests collected, no warnings about unknown markers.

- [ ] **Step 4: Final summary commit if anything was missed**

If any files were uncommitted:
```bash
git status
git add <any missed files>
git commit -m "test: jumperless integration complete"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Detection hierarchy (tests 1–6 cover all 5 cases)
- ✅ `pytest_addoption` and `pytest_configure` in `tests/conftest.py`
- ✅ `pinout.py` with `_SIGNAL_MAP`, `row()`, env-var-overridable base rows
- ✅ `jumperless` session fixture (Task 5)
- ✅ `app` function fixture (Task 7)
- ✅ `wire` autouse function fixture (Task 7)
- ✅ Retrofit: 6 low-level tests (Task 8)
- ✅ Retrofit: 4 server-style tests (Task 9, removes inline open/close)
- ✅ `test_pydwf_backend` and `test_supply` intentionally unmarked (no wiring needed)
- ✅ All 14 unit test cases from spec (Tasks 2, 4, 6)

**Placeholder scan:** None — all code steps contain complete code blocks.

**Type consistency check:**
- `row()` returns `int | str` throughout
- `connections` dict type `dict[str, tuple[str, str]]` used in `wire`
- `jumperless` fixture yields `Jumperless | None` — `wire` checks `is not None` before calling methods
- `app` fixture yields the object returned by `build_app()` — same type used in server-style tests
