from __future__ import annotations

import contextlib
import importlib
import inspect
import os
from unittest.mock import MagicMock, patch

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

def test_row_w1_natural_orientation():
    # AD3_REVERSED=0 (default): W1 at ("top", 10), AD3_TOP_ROW=1 → 1 + 10 = 11
    env = {"AD3_REVERSED": "0", "AD3_TOP_ROW": "1", "AD3_BOT_ROW": "16"}
    with patch.dict(os.environ, env):
        import tests.hardware.pinout as _p
        importlib.reload(_p)
        assert _p.row("W1") == 11


def test_row_w1_reversed():
    # AD3_REVERSED=1 (facing breadboard): W1 at ("top", 10), AD3_TOP_ROW=1 → 1 + (14-10) = 5
    env = {"AD3_REVERSED": "1", "AD3_TOP_ROW": "1", "AD3_BOT_ROW": "16"}
    with patch.dict(os.environ, env):
        import tests.hardware.pinout as _p
        importlib.reload(_p)
        assert _p.row("W1") == 5


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


# ---------------------------------------------------------------------------
# jumperless session fixture tests (tests 1-6 from spec)
# ---------------------------------------------------------------------------

def _run_jumperless(pytestconfig):
    # Unwrap and run the jumperless session fixture.
    from tests.hardware import conftest as hw
    gen_func = inspect.unwrap(hw.jumperless)
    return _run_fixture(gen_func, pytestconfig)


def test_jumperless_skip_flag_yields_none_without_probe():
    # --skip-wiring-prompts → None, no import or probe attempted
    cfg = _make_config(skip_wiring=True)
    with patch.dict("sys.modules", {"jlv5_harness": None}):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_import_error_yields_none():
    cfg = _make_config()
    with patch.dict("sys.modules", {"jlv5_harness": None}):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_fewer_than_3_ports_yields_none():
    cfg = _make_config()
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_ports.return_value = ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    with patch.dict("sys.modules", {"jlv5_harness": mock_jl_mod}):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_manual_flag_yields_none_even_with_3_ports():
    cfg = _make_config(jumperless_manual=True)
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_ports.return_value = ["/dev/cu.0", "/dev/cu.1", "/dev/cu.2"]
    with patch.dict("sys.modules", {"jlv5_harness": mock_jl_mod}):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_probe_exception_yields_none_with_warning():
    cfg = _make_config()
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_ports.side_effect = OSError("permission denied")
    with patch.dict("sys.modules", {"jlv5_harness": mock_jl_mod}), pytest.warns(
        UserWarning, match="Jumperless probe/open failed"
    ):
        val, _ = _run_jumperless(cfg)
    assert val is None


def test_jumperless_open_exception_yields_none_with_warning():
    cfg = _make_config()
    mock_jl_mod = MagicMock()
    mock_jl_mod.find_ports.return_value = ["/dev/cu.0", "/dev/cu.1", "/dev/cu.2"]
    mock_jl_mod.Harness.side_effect = RuntimeError("stale REPL")
    with patch.dict("sys.modules", {"jlv5_harness": mock_jl_mod}), pytest.warns(
        UserWarning, match="Jumperless probe/open failed"
    ):
        val, _ = _run_jumperless(cfg)
    assert val is None


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
    # _require is the 4th param (ordering dependency only; None is safe for unit tests)
    return gen_func(request, jl, cfg, None)


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
    with contextlib.suppress(StopIteration):
        next(gen)  # trigger cleanup
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
    with contextlib.suppress(RuntimeError, StopIteration):
        gen.throw(RuntimeError("test failed"))
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
        with contextlib.suppress(StopIteration):
            next(gen)
    mock_input.assert_not_called()
