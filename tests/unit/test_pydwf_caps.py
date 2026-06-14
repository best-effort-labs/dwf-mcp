import pytest

pydwf = pytest.importorskip("pydwf")
from dwf_mcp.backends.pydwf_backend import _safe_int_call


def test_safe_int_call_returns_zero_on_exception():
    def boom():
        raise RuntimeError("no analog")
    assert _safe_int_call(boom) == 0


def test_safe_int_call_passes_value():
    assert _safe_int_call(lambda: 2) == 2
