"""AD3 (Analog Discovery 3, devidDiscovery3=10) pin map and resource groups.

Verified against WaveForms SDK dwfconstants.py and SDK Python samples (2024-07-24 revision):
  - 16 general-purpose DIO pins (DIO 0–15), all 3.3 V LVCMOS (fixed; no 1.8 V option on AD3)
  - 2 AnalogIn channels → scope1/scope2 (pydwf indices 0/1)
  - 2 AnalogOut channels → awg1/awg2 (W1/W2; pydwf AnalogOutCount returns 4 on AD3 because
    the V+/V- supplies are also exposed as slow AWG ch2/3, but we control supply via AnalogIO)
  - V+ and V- power supply → vpos/vneg (AnalogIO)
  - 2 trigger I/O pins → trig1/trig2 (T1/T2; bidirectional, separate from DIO)
"""
from __future__ import annotations

from dwf_mcp.allocator import ResourceGroup

AD3_DIO_PINS: list[str] = [f"dio{i}" for i in range(16)]
AD3_ANALOG_IN_PINS: list[str] = ["scope1", "scope2"]
AD3_ANALOG_OUT_PINS: list[str] = ["awg1", "awg2"]
AD3_SUPPLY_PINS: list[str] = ["vpos", "vneg"]
AD3_TRIGGER_PINS: list[str] = ["trig1", "trig2"]

AD3_RESOURCE_GROUPS: list[ResourceGroup] = [
    # Scope channels are co-sampled — claiming one for the scope locks the pair.
    # Non-exclusive so the scope instrument can claim both channels, and so dmm (which reuses
    # AnalogIn) can claim whichever channel the scope isn't using.
    ResourceGroup(name="scope_pair", pins=set(AD3_ANALOG_IN_PINS), exclusive=False),
    # W1 and W2 share a clock domain: different instruments cannot independently own them.
    ResourceGroup(name="awg_clock", pins=set(AD3_ANALOG_OUT_PINS), exclusive=True),
]
