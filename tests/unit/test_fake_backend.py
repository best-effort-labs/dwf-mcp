from __future__ import annotations

import numpy as np
import pytest

from dwf_mcp.backend import DeviceInfo, DwfBackendError
from dwf_mcp.backends.fake import FakeBackend


def test_enumerate_finds_fake_device() -> None:
    b = FakeBackend()
    devices = b.enumerate()
    assert len(devices) == 1
    assert devices[0].serial == "FAKE-AD3-0001"
    assert devices[0].model == "Analog Discovery 3"


def test_open_close_lifecycle() -> None:
    b = FakeBackend()
    assert not b.is_open
    info = b.open()
    assert b.is_open
    assert isinstance(info, DeviceInfo)
    b.close()
    assert not b.is_open


def test_double_open_returns_same_info() -> None:
    b = FakeBackend()
    info1 = b.open()
    info2 = b.open()
    assert info1 == info2


def test_open_by_serial_matching() -> None:
    b = FakeBackend()
    b.open(serial="FAKE-AD3-0001")
    b.close()
    with pytest.raises(DwfBackendError):
        b.open(serial="DOES-NOT-EXIST")


def test_simulate_unplug() -> None:
    b = FakeBackend()
    b.open()
    b.simulate_unplug()
    assert not b.is_open


def test_scope_methods_record_calls_and_return_canned_data() -> None:
    b = FakeBackend()
    b.open()
    b.scope_configure(channel=1, range_v=5.0, offset_v=0.0, coupling="DC", enable=True)
    b.scope_configure(channel=2, range_v=5.0, offset_v=0.0, coupling="DC", enable=False)
    b.scope_set_acquisition(sample_rate_hz=1_000_000, buffer_size=1024, mode="Single")
    b.scope_set_trigger(source="detector_analog_in", channel=1, level_v=1.0,
                        condition="Rising", position_s=0.0, timeout_s=1.0)

    # Stage a canned capture: 1024 samples on channel 1, sin-ish data.
    samples = np.linspace(-1, 1, 1024, dtype=np.float64)
    b.set_scope_canned_data({1: samples})

    b.scope_arm()
    # Without explicit status progression, fake completes immediately.
    assert b.scope_status() == "Done"
    out = b.scope_read(channel=1, count=1024)
    assert np.array_equal(out, samples)

    # Verify call recording (used by Scope unit tests).
    assert b.scope_calls[0] == (
        "configure",
        {"channel": 1, "range_v": 5.0, "offset_v": 0.0, "coupling": "DC", "enable": True},
    )
    kinds = [c[0] for c in b.scope_calls]
    assert "arm" in kinds and "set_acquisition" in kinds


def test_scope_status_progression_can_be_scripted() -> None:
    b = FakeBackend()
    b.open()
    b.set_scope_status_sequence(["Armed", "Triggered", "Done"])
    assert b.scope_status() == "Armed"
    assert b.scope_status() == "Triggered"
    assert b.scope_status() == "Done"
    # After exhausting the sequence, sticks on the last value.
    assert b.scope_status() == "Done"


def test_scope_read_without_canned_returns_zeros() -> None:
    b = FakeBackend()
    b.open()
    out = b.scope_read(channel=1, count=256)
    assert out.shape == (256,)
    assert out.dtype == np.float64
    assert np.all(out == 0.0)


def test_supply_discover_returns_canned_layout() -> None:
    b = FakeBackend()
    b.open()
    layout = b.supply_discover_nodes()
    # Default canned layout exposes vpos and vneg, each with enable/voltage/current nodes.
    assert set(layout.keys()) == {"vpos", "vneg"}
    pos_ch, pos_nodes = layout["vpos"]
    assert {"enable", "voltage", "current"} <= set(pos_nodes.keys())


def test_supply_set_and_get_node_roundtrip() -> None:
    b = FakeBackend()
    b.open()
    layout = b.supply_discover_nodes()
    ch, nodes = layout["vpos"]
    b.supply_node_set(ch, nodes["voltage"], 2.5)
    # In fake, get returns what was last set (or canned measured value if scripted).
    assert b.supply_node_get(ch, nodes["voltage"]) == 2.5


def test_supply_master_enable_records() -> None:
    b = FakeBackend()
    b.open()
    b.supply_master_enable(True)
    b.supply_master_enable(False)
    enables = [c for c in b.supply_calls if c[0] == "master_enable"]
    assert [c[1]["enabled"] for c in enables] == [True, False]


def test_supply_canned_measurement_overrides_setpoint() -> None:
    b = FakeBackend()
    b.open()
    layout = b.supply_discover_nodes()
    ch, nodes = layout["vpos"]
    b.set_supply_canned_status({(ch, nodes["voltage"]): 1.97})
    b.supply_node_set(ch, nodes["voltage"], 2.0)
    assert b.supply_node_get(ch, nodes["voltage"]) == 1.97
