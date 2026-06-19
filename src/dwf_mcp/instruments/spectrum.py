# src/dwf_mcp/instruments/spectrum.py
"""Spectrum (FFT) instrument. measure() drives the scope AnalogIn capture path
directly (claims as "spectrum"); transform() FFTs an existing scope NPZ."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.spectrum_dsp import SpectrumResult, compute_spectrum, summarize_spectrum

_WINDOWS = ["rectangular", "hann", "blackman", "flattop"]
_AMPLITUDES = ["rms", "peak"]

SPECTRUM_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "sample_rate_hz", "buffer_size"],
    "properties": {
        "channel": {"type": "integer", "minimum": 1},
        "sample_rate_hz": {"type": "number", "minimum": 1.0},
        "buffer_size": {"type": "integer", "minimum": 16},
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0, "default": 5.0},
        "window": {"type": "string", "enum": _WINDOWS, "default": "hann"},
        "averaging": {"type": "integer", "minimum": 1, "default": 1},
        "amplitude": {"type": "string", "enum": _AMPLITUDES, "default": "rms"},
    },
}
SPECTRUM_MEASURE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
SPECTRUM_TRANSFORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["capture_path"],
    "properties": {
        "capture_path": {"type": "string"},
        "channel": {"type": "integer", "minimum": 1, "default": 1},
        # Optional: if omitted, read from the capture's sidecar JSON config.
        "sample_rate_hz": {"type": "number", "minimum": 1.0},
        "window": {"type": "string", "enum": _WINDOWS, "default": "hann"},
        "amplitude": {"type": "string", "enum": _AMPLITUDES, "default": "rms"},
    },
}


class Spectrum(Instrument):
    name = "spectrum"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", SPECTRUM_CONFIGURE_SCHEMA),
        "measure":   ("measure",   SPECTRUM_MEASURE_SCHEMA),
        "transform": ("transform", SPECTRUM_TRANSFORM_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None

    def configure(self, channel: int, sample_rate_hz: float, buffer_size: int,
                  range_v: float = 5.0, window: str = "hann",
                  averaging: int = 1, amplitude: str = "rms") -> dict[str, Any]:
        info = self.device.require_open()
        self.device.validate_channel(channel, "scope")
        self.device.validate_rate(sample_rate_hz)
        cap = info.analog_in_buffer_max
        if cap and buffer_size > cap:
            raise ValueError(
                f"buffer_size {buffer_size} exceeds device analog_in_buffer_max {cap}")
        if window not in _WINDOWS:
            raise ValueError(f"window must be one of {_WINDOWS}, got {window!r}")
        if amplitude not in _AMPLITUDES:
            raise ValueError(f"amplitude must be one of {_AMPLITUDES}, got {amplitude!r}")
        if averaging < 1:
            raise ValueError(f"averaging must be >= 1, got {averaging}")
        self._config = {"channel": channel, "sample_rate_hz": sample_rate_hz,
                        "buffer_size": buffer_size, "range_v": range_v,
                        "window": window, "averaging": averaging, "amplitude": amplitude}
        return {"configured": True, **self._config}

    def measure(self, output_path: str | None = None,
                description: str | None = None) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("spectrum.configure must be called before measure")
        cfg = self._config
        ch = cfg["channel"]
        info = self.device.require_open()
        be = self.device.backend
        # The AnalogIn engine is a single shared resource: scope_set_acquisition/arm are
        # global. Claim ALL analog-in channels under "spectrum" so measure() is mutually
        # exclusive with a live scope (or another spectrum) on ANY channel, not just `ch`.
        all_scope_pins = [f"scope{i}" for i in range(1, info.analog_in_channels + 1)]
        self.device.allocator.claim("spectrum", all_scope_pins)
        try:
            template: SpectrumResult | None = None
            power = None
            for _ in range(cfg["averaging"]):
                be.scope_configure(channel=ch, range_v=cfg["range_v"], offset_v=0.0,
                                   coupling="DC", enable=True)
                be.scope_set_acquisition(sample_rate_hz=cfg["sample_rate_hz"],
                                         buffer_size=cfg["buffer_size"], mode="Single")
                be.scope_arm()
                self._await_done()
                samples = be.scope_read(channel=ch, count=cfg["buffer_size"])
                res = compute_spectrum(samples, cfg["sample_rate_hz"],
                                       window=cfg["window"], amplitude=cfg["amplitude"])
                if template is None:
                    template = res
                power = res.magnitude_v ** 2 if power is None else power + res.magnitude_v ** 2
        finally:
            self.device.allocator.release("spectrum")
        # Power-domain average across captures, back to amplitude. SpectrumResult is a
        # (non-frozen) dataclass, so reuse the first capture's freq/rbw/enbw metadata.
        assert template is not None
        result = template
        result.magnitude_v = np.sqrt(power / cfg["averaging"])
        with np.errstate(divide="ignore"):
            result.magnitude_dbv = 20.0 * np.log10(np.maximum(result.magnitude_v, 1e-15))
        return self._write(result, cfg, cfg["buffer_size"], output_path, description,
                           source="measure")

    def transform(self, capture_path: str, channel: int = 1,
                  sample_rate_hz: float | None = None,
                  window: str = "hann", amplitude: str = "rms",
                  output_path: str | None = None,
                  description: str | None = None) -> dict[str, Any]:
        with np.load(capture_path) as data:
            samples = np.asarray(data[f"ch{channel}"], dtype=np.float64)
        if sample_rate_hz is None:
            sidecar = Path(capture_path).with_suffix(".json")
            if not sidecar.exists():
                raise ValueError(
                    "sample_rate_hz not given and no sidecar JSON found next to the "
                    f"capture ({sidecar}); pass sample_rate_hz explicitly")
            sample_rate_hz = float(json.loads(sidecar.read_text())["config"]["sample_rate_hz"])
        result = compute_spectrum(samples, sample_rate_hz, window=window, amplitude=amplitude)
        cfg = {"channel": channel, "sample_rate_hz": sample_rate_hz, "window": window,
               "amplitude": amplitude, "source_capture": capture_path}
        return self._write(result, cfg, int(samples.size), output_path, description,
                           source="transform")

    def _await_done(self) -> None:
        assert self._config is not None
        cfg = self._config
        deadline = time.monotonic() + max(
            cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0)
        while time.monotonic() < deadline:
            if self.device.backend.scope_status() == "Done":
                return
            time.sleep(0.002)
        raise RuntimeError("spectrum capture did not complete before deadline")

    def _write(self, result: SpectrumResult, cfg: dict[str, Any], sample_count: int,
               output_path: str | None, description: str | None,
               source: str) -> dict[str, Any]:
        summary_extra = summarize_spectrum(result)
        # sample_count = TIME samples (matches scope); bin count is len(frequency_hz).
        summary = CaptureSummary(
            instrument="spectrum", sample_count=sample_count,
            sample_rate_hz=result.sample_rate_hz, extra=summary_extra)
        res = self.artifacts.write_npz(
            instrument="spectrum",
            arrays={"frequency_hz": result.frequency_hz,
                    "magnitude_v": result.magnitude_v,
                    "magnitude_dbv": result.magnitude_dbv},
            config={**cfg, "source": source},
            summary=summary,
            output_path=Path(output_path) if output_path else None,
            description=description)
        return {"path": res.path, "sidecar_path": res.sidecar_path, "summary": summary_extra}

    def release(self) -> None:
        self.device.allocator.release("spectrum")
        self._config = None
