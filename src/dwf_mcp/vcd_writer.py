"""Thin wrapper around pyvcd for writing VCD logic capture files.

pyvcd PyPI package (pip install pyvcd) imports as `vcd`.
Optional: only used when logic format="vcd" is requested.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

try:
    import vcd as _vcd  # installed as pyvcd
    HAS_VCD = True
except ImportError:
    HAS_VCD = False


def _compute_timescale(sample_rate_hz: float) -> tuple[str, int]:
    """Return (timescale_str, time_scale_factor) for the given sample rate."""
    period_s = 1.0 / sample_rate_hz
    if period_s < 1e-9:
        return "1 ps", int(round(period_s * 1e12))
    if period_s < 1e-6:
        return "1 ns", int(round(period_s * 1e9))
    if period_s < 1e-3:
        return "1 us", int(round(period_s * 1e6))
    return "1 ms", int(round(period_s * 1e3))


def write(
    path: Path,
    samples: np.ndarray,
    pin_names: list[str],
    sample_rate_hz: float,
) -> None:
    """Write samples (uint8, shape (n_samples, n_pins)) to a VCD file."""
    if not HAS_VCD:
        raise ImportError(
            "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
        )

    n_samples, n_pins = samples.shape
    if len(pin_names) != n_pins:
        raise ValueError(
            f"pin_names length {len(pin_names)} does not match samples columns {n_pins}"
        )
    timescale, time_scale_factor = _compute_timescale(sample_rate_hz)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as f, _vcd.VCDWriter(f, timescale=timescale, date="today") as writer:
        vars_: list[Any] = [
            writer.register_var("logic", name, "wire", size=1)
            for name in pin_names
        ]
        for i, var in enumerate(vars_):
            writer.change(var, 0, int(samples[0, i]))

        prev = samples[0].copy()
        for sample_idx in range(1, n_samples):
            t = sample_idx * time_scale_factor
            row = samples[sample_idx]
            for pin_idx in range(n_pins):
                if row[pin_idx] != prev[pin_idx]:
                    writer.change(vars_[pin_idx], t, int(row[pin_idx]))
            prev = row.copy()


class VcdStreamWriter:
    """Incremental VCD writer for streaming digital capture.

    Opens the output file in __init__ and appends transitions chunk by chunk.
    Call close() (or use as a context manager) to finalize.

    Raises ImportError at construction time if pyvcd is not installed.
    """

    def __init__(self, path: Path, pin_names: list[str], sample_rate_hz: float) -> None:
        if not HAS_VCD:
            raise ImportError(
                "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
            )
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        timescale, self._time_scale_factor = _compute_timescale(sample_rate_hz)
        self._f = self._path.open("w")
        self._writer = _vcd.VCDWriter(self._f, timescale=timescale, date="today")
        self._vars: list[Any] = [
            self._writer.register_var("logic", name, "wire", size=1)
            for name in pin_names
        ]
        self._n_pins = len(pin_names)
        self._sample_counter = 0
        self._prev: np.ndarray | None = None
        self._closed = False

    def write_chunk(self, chunk: np.ndarray) -> None:
        """Append transitions from chunk (uint8, shape (N, n_pins)) to the open VCD file."""
        n_samples = chunk.shape[0]
        start_idx = self._sample_counter
        for i in range(n_samples):
            t = (start_idx + i) * self._time_scale_factor
            row = chunk[i]
            if self._prev is None:
                for pin_idx, var in enumerate(self._vars):
                    self._writer.change(var, 0, int(row[pin_idx]))
            else:
                for pin_idx in range(self._n_pins):
                    if row[pin_idx] != self._prev[pin_idx]:
                        self._writer.change(self._vars[pin_idx], t, int(row[pin_idx]))
            self._prev = row.copy()
        self._sample_counter += n_samples

    def close(self) -> None:
        """Finalize and close the VCD file. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        finally:
            self._f.close()

    def __enter__(self) -> VcdStreamWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
