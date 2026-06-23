from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.awg import AWG
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(awg_max_amplitude=3.3),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def awg(device: DwfDevice, tmp_path: Path) -> AWG:
    device.open()
    return AWG(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pin(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    assert awg.device.allocator.claimed_pins() == {"awg1": "awg"}


def test_configure_two_channels_accumulates_claims(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.configure(channel=2, function="Square", frequency_hz=500.0, amplitude_v=0.5)
    pins = awg.device.allocator.claimed_pins()
    assert pins == {"awg1": "awg", "awg2": "awg"}


def test_configure_does_not_start_output(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.awg_calls if c[0] == "start"]
    assert starts == []


def test_start_calls_backend_start(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.start(channel=1)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.awg_calls if c[0] == "start"]
    assert len(starts) == 1
    assert starts[0][1] == {"channel": 1}


def test_start_safety_gate_rejects_over_cap(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=5.0)
    with pytest.raises(SafetyViolation):
        awg.start(channel=1)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    starts = [c for c in fake.awg_calls if c[0] == "start"]
    assert starts == []  # backend never called


def test_stop_calls_backend_stop(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.stop(channel=1)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    stops = [c for c in fake.awg_calls if c[0] == "stop"]
    assert len(stops) == 1


def test_partial_failure_rollback(awg: AWG, monkeypatch: pytest.MonkeyPatch) -> None:
    # Configure ch1 successfully first.
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    prior_pins = dict(awg.device.allocator.claimed_pins())

    # Make the next configure fail at backend.
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("backend exploded")
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    monkeypatch.setattr(fake, "awg_configure", boom)

    with pytest.raises(RuntimeError):
        awg.configure(channel=2, function="Square", frequency_hz=500.0, amplitude_v=0.5)

    # Claim should be restored to prior state (ch1 only).
    assert awg.device.allocator.claimed_pins() == prior_pins


def test_upload_custom_validates_shape(awg: AWG) -> None:
    bad_samples = np.zeros((10, 2), dtype=np.float64)  # 2D, not 1D
    with pytest.raises(ValueError, match="1-D"):
        awg.upload_custom(channel=1, samples_npy_path=None, _samples=bad_samples)


def test_upload_custom_claims_pin(awg: AWG, tmp_path: Path) -> None:
    samples = np.linspace(-1.0, 1.0, 100)
    npy_path = tmp_path / "wave.npy"
    np.save(npy_path, samples)
    awg.upload_custom(channel=1, samples_npy_path=str(npy_path))
    assert "awg1" in awg.device.allocator.claimed_pins()


def test_upload_custom_stores_amplitude_for_safety_gate(awg: AWG) -> None:
    # upload_custom with amplitude_v=5.0 should trigger safety gate on start
    bad_samples = np.linspace(-1.0, 1.0, 100)
    awg.upload_custom(channel=1, samples_npy_path=None, amplitude_v=5.0, _samples=bad_samples)
    with pytest.raises(SafetyViolation):
        awg.start(channel=1)


def test_upload_custom_rejects_out_of_range_samples(awg: AWG) -> None:
    bad = np.array([0.0, 1.5, -0.5])  # 1.5 exceeds [-1, 1]
    with pytest.raises(ValueError, match=r"\[-1\.0, 1\.0\]"):
        awg.upload_custom(channel=1, samples_npy_path=None, _samples=bad)


def test_upload_custom_passes_amplitude_to_backend(awg: AWG) -> None:
    # amplitude_v must reach the backend so the custom waveform is actually scaled
    # (the backend applies it via nodeAmplitudeSet); previously it was dropped.
    samples = np.linspace(-1.0, 1.0, 16)
    awg.upload_custom(channel=1, samples_npy_path=None, amplitude_v=2.5, _samples=samples)
    fake = awg.device.backend
    call = [c for c in fake.awg_calls if c[0] == "upload_custom"][-1][1]
    assert call["amplitude_v"] == 2.5


def test_release_stops_all_channels(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.configure(channel=2, function="Square", frequency_hz=500.0, amplitude_v=0.5)
    awg.release()
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    stops = [c for c in fake.awg_calls if c[0] == "stop"]
    assert len(stops) == 2
    assert awg.device.allocator.claimed_pins() == {}


def test_reconfigure_running_channel_above_cap_is_gated(awg: AWG) -> None:
    """Reconfiguring an already-running generator applies amplitude to live
    hardware, so it must route through the safety gate — same hole as supply.set.
    """
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.start(channel=1)
    fake: FakeBackend = awg.device.backend  # type: ignore[assignment]
    boundary = len(fake.awg_calls)
    with pytest.raises(SafetyViolation):
        awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=5.0)
    # No hardware reconfigure with the over-cap amplitude.
    new_configures = [c for c in fake.awg_calls[boundary:] if c[0] == "configure"]
    assert new_configures == []
    # Stored amplitude unchanged (still the last safe value).
    assert awg._amplitude[1] == 1.0  # noqa: SLF001


def test_upload_custom_on_running_channel_above_cap_is_gated(awg: AWG) -> None:
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.start(channel=1)
    samples = np.linspace(-1.0, 1.0, 16)
    with pytest.raises(SafetyViolation):
        awg.upload_custom(channel=1, samples_npy_path=None, amplitude_v=5.0, _samples=samples)


def test_reconfigure_idle_channel_above_cap_still_allowed(awg: AWG) -> None:
    """A configured-but-not-started channel is not live, so configure() should
    keep staging without a gate (the gate fires at start). Guards against
    over-gating the running-channel fix."""
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    # Not started → reconfigure above cap is allowed (start() will reject it).
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=5.0)
    assert awg._amplitude[1] == 5.0  # noqa: SLF001


def test_stop_clears_running_so_reconfigure_allowed(awg: AWG) -> None:
    """After stop(), the channel is no longer live, so reconfigure must not gate."""
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=1.0)
    awg.start(channel=1)
    awg.stop(channel=1)
    awg.configure(channel=1, function="Sine", frequency_hz=1000.0, amplitude_v=5.0)
    assert awg._amplitude[1] == 5.0  # noqa: SLF001


def test_upload_custom_rejects_waveform_exceeding_output_buffer(tmp_path: Path) -> None:
    """A custom waveform larger than the device's AnalogOut buffer must be
    rejected (the AD1's buffer is 4096 vs the AD3's 16384)."""
    from dwf_mcp.backends.fake import make_fake_device

    dev = DwfDevice(
        backend=FakeBackend(devices=[make_fake_device(devid=2, analog_out_buffer_max=4096)]),
        policy=SafetyPolicy(awg_max_amplitude=3.3),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path, idle_timeout_s=60,
    )
    dev.open()
    awg = AWG(device=dev, artifacts=ArtifactWriter(workspace=tmp_path))
    too_big = np.linspace(-1.0, 1.0, 5000)  # > 4096
    with pytest.raises(ValueError, match="exceeds the AnalogOut buffer"):
        awg.upload_custom(channel=1, samples_npy_path=None, _samples=too_big)
    fits = np.linspace(-1.0, 1.0, 1000)
    awg.upload_custom(channel=1, samples_npy_path=None, _samples=fits)  # under cap → ok
