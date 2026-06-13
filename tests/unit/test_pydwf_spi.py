"""Unit tests for the PydwfBackend SPI translation layer.

These exercise the pydwf call *sequence* using a recording stand-in for the
ProtocolSPI object — no hardware required. They lock in the wire-level contract
that hardware loopback tests can only partially observe:

  * transfer_type is always 1 (MOSI/MISO) — never derived from assert_cs, which
    would silently drop the bus into SISO mode (transfer_type 0).
  * bits_per_word is 8 with word-count semantics (one list entry per byte), not
    len*8 crammed into the bits_per_word slot.
  * chip-select is driven explicitly via select(cs, level) around each transfer
    when assert_cs is True, and left untouched when assert_cs is False.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dwf_mcp.backends.pydwf_backend import PydwfBackend


class _RecordingSpi:
    """Records every method call as (name, args). Returns plausible values for
    the read-style calls so the backend can post-process them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def rec(*args):
            self.calls.append((name, args))
            if name.startswith("writeRead"):
                return list(args[2])  # echo tx back
            if name.startswith("read"):
                return [0] * args[2]  # number_of_words zeros
            return None
        return rec

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def backend_spi() -> tuple[PydwfBackend, _RecordingSpi]:
    backend = PydwfBackend()
    spi = _RecordingSpi()
    backend._device = SimpleNamespace(protocol=SimpleNamespace(spi=spi))  # noqa: SLF001
    return backend, spi


def _configure(backend: PydwfBackend, cs_idx: int | None = 3,
               cs_polarity: str = "active_low") -> None:
    backend.spi_configure(
        clk_idx=0, freq_hz=1_000_000, mode=0,
        mosi_idx=1, miso_idx=2, cs_idx=cs_idx,
        cs_polarity=cs_polarity, bit_order="msb",
    )


# --- transfer_type / bits_per_word ------------------------------------------

def test_transfer_uses_mosi_miso_and_word_semantics(backend_spi) -> None:
    backend, spi = backend_spi
    _configure(backend)
    spi.calls.clear()
    backend.spi_transfer(bytes([0xAA, 0xBB, 0xCC]), assert_cs=False)
    wr = [c for c in spi.calls if c[0] == "writeRead"][0]
    transfer_type, bits_per_word, tx = wr[1]
    assert transfer_type == 1
    assert bits_per_word == 8
    assert list(tx) == [0xAA, 0xBB, 0xCC]


def test_write_uses_standard_transfer_type_even_when_cs_false(backend_spi) -> None:
    """Regression: write() used to pass (1 if assert_cs else 0) as transfer_type,
    silently selecting SISO mode when assert_cs was False."""
    backend, spi = backend_spi
    _configure(backend)
    spi.calls.clear()
    backend.spi_write(bytes([0x01, 0x02, 0x03, 0x04, 0x05]), assert_cs=False)
    w = [c for c in spi.calls if c[0] == "write"][0]
    transfer_type, bits_per_word, tx = w[1]
    assert transfer_type == 1          # not 0 (SISO)
    assert bits_per_word == 8          # not len*8 (would be 40 → invalid)
    assert list(tx) == [0x01, 0x02, 0x03, 0x04, 0x05]


def test_read_passes_word_count_not_total_bits(backend_spi) -> None:
    """Regression: read() used to call read(dcs, length*8) — missing the
    number_of_words argument entirely and mis-using bits_per_word."""
    backend, spi = backend_spi
    _configure(backend)
    spi.calls.clear()
    out = backend.spi_read(4, assert_cs=False)
    r = [c for c in spi.calls if c[0] == "read"][0]
    transfer_type, bits_per_word, number_of_words = r[1]
    assert transfer_type == 1
    assert bits_per_word == 8
    assert number_of_words == 4
    assert len(out) == 4


# --- chip-select bracketing --------------------------------------------------

def test_transfer_brackets_cs_active_low_when_asserted(backend_spi) -> None:
    backend, spi = backend_spi
    _configure(backend, cs_idx=3, cs_polarity="active_low")
    spi.calls.clear()
    backend.spi_transfer(bytes([0xAA]), assert_cs=True)
    assert spi.names() == ["select", "writeRead", "select"]
    assert spi.calls[0][1] == (3, 0)   # assert: active-low → drive 0
    assert spi.calls[2][1] == (3, 1)   # release: → drive 1


def test_transfer_no_cs_calls_when_not_asserted(backend_spi) -> None:
    backend, spi = backend_spi
    _configure(backend, cs_idx=3)
    spi.calls.clear()
    backend.spi_transfer(bytes([0xAA]), assert_cs=False)
    assert spi.names() == ["writeRead"]   # CS untouched


def test_transfer_active_high_cs_levels(backend_spi) -> None:
    backend, spi = backend_spi
    _configure(backend, cs_idx=5, cs_polarity="active_high")
    spi.calls.clear()
    backend.spi_transfer(bytes([0xAA]), assert_cs=True)
    assert spi.calls[0][1] == (5, 1)   # assert active-high → 1
    assert spi.calls[-1][1] == (5, 0)  # release → 0


def test_cs_released_even_if_transfer_raises(backend_spi) -> None:
    """CS must be returned to idle even if the underlying transfer throws,
    otherwise the bus is left with a slave selected."""
    backend, spi = backend_spi
    _configure(backend, cs_idx=3, cs_polarity="active_low")

    boom_calls: list[tuple] = []

    def boom_writeRead(*args):
        boom_calls.append(args)
        raise RuntimeError("transfer failed")

    spi.writeRead = boom_writeRead  # type: ignore[assignment]
    spi.calls.clear()
    with pytest.raises(RuntimeError):
        backend.spi_transfer(bytes([0xAA]), assert_cs=True)
    # The trailing select(cs, release) must still have run.
    selects = [c for c in spi.calls if c[0] == "select"]
    assert selects[-1][1] == (3, 1)
