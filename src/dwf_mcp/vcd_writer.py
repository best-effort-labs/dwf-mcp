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


def write(
    path: Path,
    samples: np.ndarray,
    pin_names: list[str],
    sample_rate_hz: float,
) -> None:
    """Write samples (uint8, shape (n_samples, n_pins)) to a VCD file.

    Raises ImportError if pyvcd is not installed.
    """
    if not HAS_VCD:
        raise ImportError(
            "VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"
        )

    n_samples, n_pins = samples.shape
    # Compute timescale: pick ns or us depending on sample rate.
    # 1 sample = 1/sample_rate_hz seconds.
    period_s = 1.0 / sample_rate_hz
    if period_s < 1e-9:
        timescale = "1 ps"
        time_scale_factor = int(round(period_s * 1e12))
    elif period_s < 1e-6:
        timescale = "1 ns"
        time_scale_factor = int(round(period_s * 1e9))
    elif period_s < 1e-3:
        timescale = "1 us"
        time_scale_factor = int(round(period_s * 1e6))
    else:
        timescale = "1 ms"
        time_scale_factor = int(round(period_s * 1e3))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as f:
        with _vcd.VCDWriter(f, timescale=timescale, date="today") as writer:
            # Register one variable per pin.
            vars_: list[Any] = [
                writer.register_var("logic", name, "wire", size=1)
                for name in pin_names
            ]
            # Emit initial values at time 0.
            for i, var in enumerate(vars_):
                writer.change(var, 0, int(samples[0, i]))

            # Iterate samples, emit only on transitions.
            prev = samples[0].copy()
            for sample_idx in range(1, n_samples):
                t = sample_idx * time_scale_factor
                row = samples[sample_idx]
                for pin_idx in range(n_pins):
                    if row[pin_idx] != prev[pin_idx]:
                        writer.change(vars_[pin_idx], t, int(row[pin_idx]))
                prev = row.copy()
