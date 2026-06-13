from __future__ import annotations

import asyncio  # noqa: F401
import json
import time
from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DwfDeviceLost
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


def test_open_returns_device_info(device: DwfDevice) -> None:
    info = device.open()
    assert info.model == "Analog Discovery 3"
    assert device.is_open


def test_open_is_idempotent(device: DwfDevice) -> None:
    info1 = device.open()
    info2 = device.open()
    assert info1 == info2


def test_close_releases_handle_and_pins(device: DwfDevice) -> None:
    device.open()
    device.allocator.claim("i2c", ["dio0", "dio1"])
    device.close()
    assert not device.is_open
    assert device.allocator.claimed_pins() == {}


def test_status_reports_open_state(device: DwfDevice) -> None:
    status = device.status()
    assert status["open"] is False

    device.open()
    device.allocator.claim("i2c", ["dio0", "dio1"])
    status = device.status()
    assert status["open"] is True
    assert status["device"]["serial"] == "FAKE-AD3-0001"
    assert status["claimed_pins"] == {"dio0": "i2c", "dio1": "i2c"}
    assert status["claimed_instruments"] == ["i2c"]


def test_hot_unplug_marks_session_dead(device: DwfDevice) -> None:
    device.open()
    device.backend.simulate_unplug()  # type: ignore[attr-defined]
    # require_open should now raise
    with pytest.raises(DwfDeviceLost):
        device.require_open()


def test_require_open_returns_info_when_alive(device: DwfDevice) -> None:
    device.open()
    info = device.require_open()
    assert info.model == "Analog Discovery 3"


def test_idle_close_after_timeout(tmp_path) -> None:
    backend = FakeBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=0.05,
    )
    device.open()
    assert device.is_open
    time.sleep(0.15)
    device.tick_idle()  # caller invokes between tool calls
    assert not device.is_open


def test_activity_resets_idle_timer(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=0.2,
    )
    device.open()
    time.sleep(0.1)
    device.mark_activity()
    time.sleep(0.15)
    device.tick_idle()
    assert device.is_open


def test_gate_output_supply_pos_within_cap(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.gate_output("supply_enable", channel="pos", voltage=3.0)
    log_path = tmp_path / "dwf-safety.log"
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert lines[-1]["kind"] == "supply_enable"
    assert lines[-1]["params"]["voltage"] == 3.0


def test_gate_output_supply_pos_over_cap_raises(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    with pytest.raises(SafetyViolation) as exc:
        device.gate_output("supply_enable", channel="pos", voltage=5.0)
    assert "5.0" in str(exc.value)
    # Rejection is also logged (for audit), with rejected=True
    log_lines = (tmp_path / "dwf-safety.log").read_text().splitlines()
    lines = [json.loads(line) for line in log_lines if line.strip()]
    assert lines[-1]["rejected"] is True


def test_gate_output_supply_current(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_current=0.5),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.gate_output("supply_enable", channel="pos", voltage=3.0, current_limit=0.4)
    with pytest.raises(SafetyViolation):
        device.gate_output("supply_enable", channel="pos", voltage=3.0, current_limit=0.6)


def test_gate_output_unknown_kind_passes_through(tmp_path) -> None:
    # Kinds we don't recognize don't get policy checks — they still log.
    # This preserves forward-compat with future kinds added in stage 3.
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.gate_output("future_kind", foo="bar")
    log_lines = (tmp_path / "dwf-safety.log").read_text().splitlines()
    lines = [json.loads(line) for line in log_lines if line.strip()]
    assert lines[-1]["kind"] == "future_kind"


def test_gate_output_supply_enable_missing_channel_raises(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    with pytest.raises(SafetyViolation) as exc:
        device.gate_output("supply_enable", voltage=3.0)
    assert "channel" in str(exc.value)


def test_gate_output_supply_enable_missing_voltage_raises(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    with pytest.raises(SafetyViolation) as exc:
        device.gate_output("supply_enable", channel="pos")
    assert "voltage" in str(exc.value)


def test_gate_output_supply_enable_bad_current_limit_raises(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    with pytest.raises(SafetyViolation):
        device.gate_output(
            "supply_enable", channel="pos", voltage=3.0, current_limit="not-a-number"
        )


def test_gate_output_awg_start_missing_amplitude_raises(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    with pytest.raises(SafetyViolation) as exc:
        device.gate_output("awg_start")
    assert "amplitude" in str(exc.value)


def test_gate_output_log_failure_does_not_mask_safety_violation(tmp_path, monkeypatch) -> None:
    """If the safety log file write fails, the in-flight SafetyViolation must still propagate."""
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    # Make any file open fail with IOError.
    original_open = Path.open
    def boom_open(self, *args, **kwargs):
        raise OSError("disk on fire")
    monkeypatch.setattr(Path, "open", boom_open)
    with pytest.raises(SafetyViolation):
        device.gate_output("supply_enable", channel="pos", voltage=5.0)
    monkeypatch.setattr(Path, "open", original_open)


def test_gate_output_with_empty_workspace_does_not_crash(caplog) -> None:
    """Empty workspace falls back to logger-only audit; doesn't crash, doesn't write CWD."""
    import logging
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(),
        workspace="",
        idle_timeout_s=60,
    )
    with caplog.at_level(logging.INFO, logger="dwf_mcp.device"):
        device.gate_output("supply_enable", channel="pos", voltage=3.0)
    # The audit went to the logger, not a file.
    assert any("safety event (no workspace)" in r.message for r in caplog.records)


def test_gate_output_supply_neg_over_cap_raises(tmp_path) -> None:
    """Symmetry: neg channel also enforced."""
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_neg=-3.3),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.gate_output("supply_enable", channel="neg", voltage=-3.0)  # within cap
    with pytest.raises(SafetyViolation):
        device.gate_output("supply_enable", channel="neg", voltage=-5.0)


def _md_device(tmp_path):
    return DwfDevice(backend=FakeBackend(), policy=SafetyPolicy(),
                     allocator=PinAllocator(), workspace=tmp_path, idle_timeout_s=60)


def test_open_configures_allocator_from_profile(tmp_path) -> None:
    d = _md_device(tmp_path)
    d.open()
    names = {g.name for g in d.allocator.resource_groups}
    assert names == {"scope_pair", "awg_clock"}
    assert d.profile.name == "Analog Discovery 3"
    assert d.inventory.is_valid_pin("dio15")


def test_close_resets_allocator_configuration(tmp_path) -> None:
    d = _md_device(tmp_path)
    d.open()
    d.close()
    assert d.allocator.resource_groups == []
    assert d.profile is None


def test_md_validate_pin_rejects_out_of_range(tmp_path) -> None:
    d = _md_device(tmp_path)
    d.open()
    d.validate_pin("dio15")  # ok
    with pytest.raises(ValueError, match="dio16"):
        d.validate_pin("dio16")


def test_md_validate_rate_uses_queried_max(tmp_path) -> None:
    d = _md_device(tmp_path)
    d.open()
    d.validate_rate(100_000_000.0)  # ok (fake reports 100 MHz)
    with pytest.raises(ValueError, match="exceeds"):
        d.validate_rate(125_000_000.0)


def test_open_unknown_devid_closes_backend(tmp_path) -> None:
    from dwf_mcp.backends.fake import make_fake_device
    from dwf_mcp.devices.profiles import UnsupportedDeviceError
    backend = FakeBackend(devices=[make_fake_device(devid=99)])
    d = DwfDevice(backend=backend, policy=SafetyPolicy(),
                  allocator=PinAllocator(), workspace=tmp_path, idle_timeout_s=60)
    with pytest.raises(UnsupportedDeviceError):
        d.open()
    assert not backend.is_open  # open-failure cleanup closed it
    assert d.profile is None


def test_close_fires_on_close_hook(tmp_path) -> None:
    d = _md_device(tmp_path)
    fired = {"n": 0}
    d.on_close = lambda: fired.__setitem__("n", fired["n"] + 1)
    d.open()
    d.close()
    assert fired["n"] == 1
