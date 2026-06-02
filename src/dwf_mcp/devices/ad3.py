"""AD3 pin map and resource groups. Refine against the AD3 reference manual before stage 2."""
from __future__ import annotations

from dwf_mcp.allocator import ResourceGroup

# Provisional. Confirm against AD3 reference manual when wiring real instruments in stage 2.
AD3_DIO_PINS: list[str] = [f"dio{i}" for i in range(16)]
AD3_ANALOG_IN_PINS: list[str] = ["scope1", "scope2"]
AD3_ANALOG_OUT_PINS: list[str] = ["awg1", "awg2"]
AD3_SUPPLY_PINS: list[str] = ["vpos", "vneg"]
AD3_TRIGGER_PINS: list[str] = ["trig1", "trig2"]

AD3_RESOURCE_GROUPS: list[ResourceGroup] = [
    # Scope channels are co-sampled — claiming one for the scope locks the pair.
    # Marked non-exclusive here so the *same* instrument can claim both; cross-instrument
    # conflict is still caught because pin ownership is per-pin.
    ResourceGroup(name="scope_pair", pins=set(AD3_ANALOG_IN_PINS), exclusive=False),
    # AWG channels share a clock domain. Two different instruments can't both drive AWG outputs.
    ResourceGroup(name="awg_clock", pins=set(AD3_ANALOG_OUT_PINS), exclusive=True),
]
