from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.policy import SafetyPolicy


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def scope(device: DwfDevice, tmp_path: Path) -> Scope:
    device.open()
    return Scope(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pins_and_records_backend_calls(scope: Scope) -> None:
    scope.configure(channels=[1, 2], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    assert scope.device.allocator.claimed_pins() == {"scope1": "scope", "scope2": "scope"}
    fake = scope.device.backend  # type: ignore[assignment]
    kinds = [c[0] for c in fake.scope_calls]  # type: ignore[attr-defined]
    assert kinds.count("configure") == 2  # both channels
    assert "set_acquisition" in kinds


def test_reconfigure_releases_old_pin_claims(scope: Scope) -> None:
    scope.configure(channels=[1, 2], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    assert scope.device.allocator.claimed_pins() == {"scope1": "scope"}


def test_set_trigger_without_configure_raises(scope: Scope) -> None:
    with pytest.raises(InstrumentNotConfigured):
        scope.set_trigger(source="detector_analog_in", channel=1, level_v=1.0,
                          condition="Rising", position_s=0.0, timeout_s=1.0)


def test_capture_without_configure_raises(scope: Scope) -> None:
    with pytest.raises(InstrumentNotConfigured):
        scope.capture()


def test_capture_returns_path_sidecar_summary(scope: Scope, tmp_path: Path) -> None:
    # Stage canned samples: a 1kHz-ish sine at 1MS/s, 1024 samples.
    t = np.linspace(0, 1024 / 1_000_000, 1024, endpoint=False)
    sine = np.sin(2 * np.pi * 1000 * t)
    scope.device.backend.set_scope_canned_data({1: sine})  # type: ignore[attr-defined]
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    result = scope.capture()
    assert Path(result["path"]).is_file()
    assert Path(result["sidecar_path"]).is_file()
    summary = result["summary"]
    assert "ch1" in summary
    assert abs(summary["ch1"]["min"] - (-1.0)) < 0.01
    assert abs(summary["ch1"]["max"] - 1.0) < 0.01
    assert abs(summary["ch1"]["rms"] - (1 / np.sqrt(2))) < 0.05
    # Freq estimate within 10% (rough zero-crossing).
    assert 900 < summary["ch1"]["freq_estimate"] < 1100
    sidecar = json.loads(Path(result["sidecar_path"]).read_text())
    assert sidecar["config"]["channels"] == [1]


def test_capture_polls_status_until_done(scope: Scope) -> None:
    scope.device.backend.set_scope_status_sequence(  # type: ignore[attr-defined]
        ["Armed", "Armed", "Triggered", "Done"]
    )
    scope.device.backend.set_scope_canned_data({1: np.zeros(1024)})  # type: ignore[attr-defined]
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    result = scope.capture()
    assert "path" in result


def test_release_clears_pin_claims(scope: Scope) -> None:
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    scope.release()
    assert scope.device.allocator.claimed_pins() == {}


def test_summarize_empty_array_does_not_crash() -> None:
    """A misbehaving backend returning an empty array must not crash _summarize."""
    result = Scope._summarize(np.array([], dtype=np.float64), sample_rate_hz=1_000_000)
    assert result == {"min": 0.0, "max": 0.0, "mean": 0.0, "rms": 0.0,
                      "freq_estimate": 0.0, "sample_rate": 1_000_000}


def test_configure_backend_failure_leaves_instrument_unconfigured(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a backend call raises mid-configure, state must roll back.

    Pins must be released and _config must be None.
    Tests the partial-failure pattern Supply and I2C will reuse.
    """
    # First configure succeeds.
    scope.configure(channels=[1, 2], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    assert scope._config is not None  # noqa: SLF001
    # Now make scope_set_acquisition raise during a reconfigure.
    backend = scope.device.backend
    def boom_set_acq(**kwargs: object) -> None:
        raise RuntimeError("backend on fire")
    monkeypatch.setattr(backend, "scope_set_acquisition", boom_set_acq)
    with pytest.raises(RuntimeError):
        scope.configure(channels=[1], range_v=10.0, offset_v=0.0, coupling="DC",
                        sample_rate_hz=2_000_000, buffer_size=2048)
    # _config should be None (cleared before backend call, not restored).
    assert scope._config is None  # noqa: SLF001
    # Pins released — no stale scope1 claim from the failed reconfigure.
    assert scope.device.allocator.claimed_pins() == {}


def test_configure_first_call_failure_does_not_leak_pins(device: DwfDevice, tmp_path: Path) -> None:
    """If a fresh configure (no prior state) fails mid-backend, pins must be released."""
    from dwf_mcp.artifacts import ArtifactWriter
    device.open()  # validate_channel needs live inventory
    scope = Scope(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
    backend = scope.device.backend
    original = backend.scope_configure
    call_count = {"n": 0}
    def boom_after_first(**kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("backend on fire")
        original(**kwargs)
    backend.scope_configure = boom_after_first  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        scope.configure(channels=[1, 2], range_v=5.0, offset_v=0.0, coupling="DC",
                        sample_rate_hz=1_000_000, buffer_size=1024)
    assert scope.device.allocator.claimed_pins() == {}
    assert scope._config is None  # noqa: SLF001
