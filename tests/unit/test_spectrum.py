# tests/unit/test_spectrum.py
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocationError, PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.instruments.spectrum import Spectrum
from dwf_mcp.policy import SafetyPolicy


def _dev(tmp_path) -> DwfDevice:
    d = DwfDevice(backend=FakeBackend(), policy=SafetyPolicy(),
                  allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
                  workspace=tmp_path, idle_timeout_s=60)
    d.open()
    return d


def _canned_sine(be: FakeBackend, freq, sr, n, amp=1.0):
    t = np.arange(n) / sr
    be.set_scope_canned_data({1: amp * np.sin(2 * np.pi * freq * t)})


def test_measure_returns_peak_and_artifact(tmp_path: Path):
    d = _dev(tmp_path)
    be: FakeBackend = d.backend  # type: ignore[assignment]
    sr, n = 100_000.0, 4096
    _canned_sine(be, 50 * sr / n, sr, n, amp=1.0)
    spec = Spectrum(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    spec.configure(channel=1, sample_rate_hz=sr, buffer_size=n,
                   window="rectangular", amplitude="peak")
    out = spec.measure()
    assert out["summary"]["peak_frequency_hz"] == pytest.approx(50 * sr / n, rel=0.01)
    assert out["summary"]["peak_magnitude_dbv"] == pytest.approx(0.0, abs=0.3)
    assert Path(out["path"]).exists() and Path(out["sidecar_path"]).exists()


def test_buffer_size_validated_against_device_cap(tmp_path: Path):
    d = _dev(tmp_path)  # fake analog_in_buffer_max defaults to 16384
    spec = Spectrum(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    with pytest.raises(ValueError, match="buffer_size"):
        spec.configure(channel=1, sample_rate_hz=100_000.0, buffer_size=10_000_000)


def test_measure_releases_claim_when_done(tmp_path: Path):
    # measure() claims the AnalogIn engine for the duration, then releases it.
    d = _dev(tmp_path)
    _canned_sine(d.backend, 1000.0, 100_000.0, 4096)  # type: ignore[arg-type]
    spec = Spectrum(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    spec.configure(channel=1, sample_rate_hz=100_000.0, buffer_size=4096)
    spec.measure()
    assert d.allocator.claimed_pins() == {}  # released after measure


def test_measure_conflicts_with_live_scope(tmp_path: Path):
    # A live scope owns the AnalogIn engine; spectrum.measure() must be refused
    # (it claims ALL analog-in pins under "spectrum" since acquisition is global).
    d = _dev(tmp_path)
    _canned_sine(d.backend, 1000.0, 100_000.0, 4096)  # type: ignore[arg-type]
    scope = Scope(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    scope.configure(channels=[2], range_v=5.0, sample_rate_hz=100_000.0, buffer_size=1024)
    spec = Spectrum(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    spec.configure(channel=1, sample_rate_hz=100_000.0, buffer_size=4096)
    with pytest.raises(PinAllocationError):
        spec.measure()  # scope2 held by "scope" -> claim of all scope pins fails


def test_transform_explicit_sample_rate(tmp_path: Path):
    d = _dev(tmp_path)
    sr, n = 100_000.0, 4096
    samples = np.sin(2 * np.pi * (50 * sr / n) * np.arange(n) / sr)
    npz = tmp_path / "cap.npz"
    np.savez_compressed(npz, ch1=samples)
    spec = Spectrum(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    out = spec.transform(capture_path=str(npz), channel=1, sample_rate_hz=sr,
                         window="rectangular", amplitude="peak")
    assert out["summary"]["peak_frequency_hz"] == pytest.approx(50 * sr / n, rel=0.01)


def test_transform_reads_sample_rate_from_sidecar(tmp_path: Path):
    # When sample_rate_hz is omitted, transform() reads it from the scope sidecar JSON.
    d = _dev(tmp_path)
    sr, n = 100_000.0, 4096
    samples = np.sin(2 * np.pi * (50 * sr / n) * np.arange(n) / sr)
    npz = tmp_path / "cap.npz"
    np.savez_compressed(npz, ch1=samples)
    npz.with_suffix(".json").write_text(json.dumps({"config": {"sample_rate_hz": sr}}))
    spec = Spectrum(device=d, artifacts=ArtifactWriter(workspace=tmp_path))
    out = spec.transform(capture_path=str(npz), channel=1, window="rectangular", amplitude="peak")
    assert out["summary"]["peak_frequency_hz"] == pytest.approx(50 * sr / n, rel=0.01)
