"""Device configuration selection.

WaveForms devices expose several hardware *configurations* that partition FPGA
resources differently — e.g. a big DigitalIn record buffer at the cost of smaller
output buffers. On the original Analog Discovery / AD2 (shared IOs) these are real
tradeoffs; the AD3 (independent IOs) can max everything in its default config.

The configuration is fixed at device *open*, so we resolve a caller-supplied
*strategy* (declared by intent) to a concrete config index here, by querying the
device's config table. This keeps the choice device-agnostic: the caller asks for
``max_digital_in`` when about to sniff/log at high rate, without knowing that this
is config 7 on an AD2 but config 3 on an AD3.
"""
from __future__ import annotations

from dataclasses import dataclass

# Accepted device_config strategies (also surfaced to the MCP client).
CONFIG_STRATEGIES = ("default", "max_digital_in", "max_analog_in")


@dataclass(frozen=True)
class DeviceConfig:
    index: int
    digital_in_buffer: int
    analog_in_buffer: int
    analog_out_buffer: int
    digital_out_buffer: int


def resolve_config_index(configs: list[DeviceConfig], strategy: str | None) -> int | None:
    """Pick a config index for a strategy. Returns None to mean "SDK default"
    (don't force a specific config). ``max_digital_in`` maximizes the DigitalIn
    buffer (tie-broken by AnalogIn); ``max_analog_in`` is the mirror image."""
    if strategy in (None, "default"):
        return None
    if not configs:
        return None
    if strategy == "max_digital_in":
        best = max(configs, key=lambda c: (c.digital_in_buffer, c.analog_in_buffer))
        return best.index
    if strategy == "max_analog_in":
        best = max(configs, key=lambda c: (c.analog_in_buffer, c.digital_in_buffer))
        return best.index
    raise ValueError(
        f"unknown device_config strategy {strategy!r}; expected one of {CONFIG_STRATEGIES}"
    )
