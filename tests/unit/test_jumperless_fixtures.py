from __future__ import annotations

import importlib
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

def test_row_w1_flipped_default():
    # AD3_FLIP=1 (default): W1 at ("top", 10), AD3_TOP_ROW=1 → 1 + (14-10) = 5
    env = {"AD3_FLIP": "1", "AD3_TOP_ROW": "1", "AD3_BOT_ROW": "16"}
    with patch.dict(os.environ, env):
        import tests.hardware.pinout as _p
        importlib.reload(_p)
        assert _p.row("W1") == 5


def test_row_w1_no_flip():
    # AD3_FLIP=0 (face-first via ribbon): W1 at ("top", 10), AD3_TOP_ROW=1 → 1 + 10 = 11
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
