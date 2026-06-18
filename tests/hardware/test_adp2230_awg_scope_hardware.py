"""ADP2230 AWG -> scope analog-path check over a manual BNC cable (W1 -> scope CHn).

This needs a physical BNC cable that the Jumperless can't route, so it's **opt-in**:
set ADP_AWG_SCOPE_CHANNELS to the scope channel(s) you've cabled W1 to, e.g.
  ADP_AWG_SCOPE_CHANNELS=1            # W1 -> CH1
  ADP_AWG_SCOPE_CHANNELS=2            # W1 -> CH2
  ADP_AWG_SCOPE_CHANNELS=1,2         # both (needs a BNC tee, or run twice & move the cable)
Unset (default) -> both parametrizations skip. Marked `wired` so it joins the
wired toggle (`-m "not wired"` excludes it), but it does NOT use the Jumperless, so
the no-Jumperless auto-skip does not apply — the env opt-in is its only gate.

Run: DWF_TEST_SERIAL=210417BAF36D ADP_AWG_SCOPE_CHANNELS=1 \\
     .venv/bin/pytest tests/hardware/test_adp2230_awg_scope_hardware.py -m hardware -v
"""
from __future__ import annotations

import os

import pytest

# Scope channels the caller has cabled W1 to (e.g. "1", "2", "1,2"); empty = opt-out.
_CABLED_CHANNELS = {
    int(c) for c in os.environ.get("ADP_AWG_SCOPE_CHANNELS", "").replace(",", " ").split()
}


def _is_adp2230(device) -> bool:
    return device.profile is not None and device.profile.devid == 14


@pytest.mark.hardware
@pytest.mark.wired
@pytest.mark.requires(instruments={"awg", "scope"})
@pytest.mark.parametrize("scope_ch", [1, 2])
def test_adp2230_awg_to_scope(device, artifacts, scope_ch) -> None:
    """Drive W1 with a 1 kHz sine and confirm scope CH`scope_ch` captures it."""
    if not _is_adp2230(device):
        pytest.skip("DUT is not an ADP2230")
    if scope_ch not in _CABLED_CHANNELS:
        pytest.skip(
            f"scope CH{scope_ch} not opted in; set ADP_AWG_SCOPE_CHANNELS "
            f"(e.g. '1', '2', '1,2') once W1 is cabled to that scope input"
        )
    from dwf_mcp.instruments.awg import AWG
    from dwf_mcp.instruments.scope import Scope

    awg = AWG(device=device, artifacts=artifacts)
    scope = Scope(device=device, artifacts=artifacts)
    try:
        awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
        awg.start(channel=1)
        scope.configure(channels=[scope_ch], range_v=5.0, sample_rate_hz=100_000,
                        buffer_size=4096)
        scope.set_trigger(source="detector_analog_in", channel=scope_ch, level_v=0.0,
                          condition="Rising", timeout_s=2.0)
        result = scope.capture()
        summary = result["summary"][f"ch{scope_ch}"]
        freq = summary["freq_estimate"]
        vpp = summary["max"] - summary["min"]
        assert 900 < freq < 1100, f"CH{scope_ch}: expected ~1000 Hz, got {freq} ({summary})"
        assert 1.6 < vpp < 2.4, f"CH{scope_ch}: expected ~2 Vpp, got {vpp} ({summary})"
    finally:
        awg.stop(channel=1)
