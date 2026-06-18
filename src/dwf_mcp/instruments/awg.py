"""AWG (AnalogOut) instrument. Channel count is device-dependent (W1/W2 on classic
AD, W1 only on ADP2230); validated per call via device.validate_channel. Accumulating
pin claim model."""
from __future__ import annotations

import contextlib
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_Set = set  # alias — 'set' is a method name on this class

_VALID_FUNCTIONS = frozenset(
    {"Sine", "Square", "Triangle", "RampUp", "RampDown", "DC", "Noise", "Custom"}
)
_CHANNEL_TO_PIN = {1: "awg1", 2: "awg2"}

AWG_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "function", "frequency_hz", "amplitude_v"],
    "properties": {
        "channel": {"type": "integer", "minimum": 1},
        "function": {
            "type": "string",
            "enum": sorted(_VALID_FUNCTIONS),
        },
        "frequency_hz": {"type": "number", "minimum": 0.0},
        "amplitude_v": {"type": "number", "minimum": 0.0},
        "offset_v": {"type": "number", "default": 0.0},
        "phase_deg": {"type": "number", "default": 0.0},
        "symmetry": {"type": "number", "minimum": 0.0, "maximum": 100.0, "default": 50.0},
        "run_time_s": {"type": "number", "minimum": 0.0},
    },
}

AWG_UPLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "samples_npy_path"],
    "properties": {
        "channel": {"type": "integer", "minimum": 1},
        "samples_npy_path": {"type": "string"},
        "amplitude_v": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}

AWG_CHANNEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel"],
    "properties": {"channel": {"type": "integer", "minimum": 1}},
}


class AWG(Instrument):
    name = "awg"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":     ("configure",     AWG_CONFIGURE_SCHEMA),
        "upload_custom": ("upload_custom", AWG_UPLOAD_SCHEMA),
        "start":         ("start",         AWG_CHANNEL_SCHEMA),
        "stop":          ("stop",          AWG_CHANNEL_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._amplitude: dict[int, float] = {}
        self._configured_channels: _Set[int] = set()
        self._running_channels: _Set[int] = set()

    def configure(
        self,
        channel: int,
        function: str,
        frequency_hz: float,
        amplitude_v: float,
        offset_v: float = 0.0,
        phase_deg: float = 0.0,
        symmetry: float = 50.0,
        run_time_s: float | None = None,
    ) -> dict[str, Any]:
        if function not in _VALID_FUNCTIONS:
            raise ValueError(f"function must be one of {sorted(_VALID_FUNCTIONS)}, got {function!r}")
        self.device.validate_channel(channel, "awg")
        pin = _CHANNEL_TO_PIN[channel]
        # Reconfiguring a running channel applies the new amplitude to live
        # hardware, so gate it like start() before any hardware write. Idle
        # channels stay ungated (the gate fires at start).
        if channel in self._running_channels:
            self.device.gate_output("awg_start", channel=channel, amplitude=amplitude_v)
        prior_channels = _Set(self._configured_channels)
        prior_amplitude = self._amplitude.get(channel)
        new_pins = sorted(_CHANNEL_TO_PIN[c] for c in (prior_channels | {channel}))
        self.device.allocator.claim("awg", new_pins)
        self._configured_channels.discard(channel)
        self._amplitude.pop(channel, None)
        try:
            self.device.backend.awg_configure(
                channel=channel,
                function=function,
                freq_hz=frequency_hz,
                amplitude_v=amplitude_v,
                offset_v=offset_v,
                phase_deg=phase_deg,
                symmetry=symmetry,
                run_time_s=run_time_s,
            )
        except Exception:
            if prior_channels:
                prior_pins = sorted(_CHANNEL_TO_PIN[c] for c in prior_channels)
                self.device.allocator.claim("awg", prior_pins)
            else:
                self.device.allocator.release("awg")
            if prior_amplitude is not None:
                self._amplitude[channel] = prior_amplitude
            self._configured_channels = prior_channels
            raise
        self._configured_channels.add(channel)
        self._amplitude[channel] = amplitude_v
        return {"configured": True, "channel": channel, "pin": pin}

    def upload_custom(
        self,
        channel: int,
        samples_npy_path: str | None,
        amplitude_v: float = 1.0,
        _samples: np.ndarray | None = None,  # for unit testing without a file
    ) -> dict[str, Any]:
        self.device.validate_channel(channel, "awg")
        if _samples is not None:
            samples = _samples
        else:
            if samples_npy_path is None:
                raise ValueError("samples_npy_path required")
            samples = np.load(samples_npy_path)
        if samples.ndim != 1:
            raise ValueError(f"samples must be 1-D, got shape {samples.shape}")
        samples = np.asarray(samples, dtype=np.float64)
        if samples.size > 0 and (samples.min() < -1.0 or samples.max() > 1.0):
            raise ValueError(
                f"custom waveform samples must be in [-1.0, 1.0], "
                f"got range [{float(samples.min()):.3f}, {float(samples.max()):.3f}]"
            )
        self.device.validate_awg_samples(len(samples))
        # Re-uploading to a running channel applies the new amplitude to live
        # hardware — gate it like start() before any hardware write.
        if channel in self._running_channels:
            self.device.gate_output("awg_start", channel=channel, amplitude=amplitude_v)
        pin = _CHANNEL_TO_PIN[channel]
        prior_channels = _Set(self._configured_channels)
        new_pins = sorted(_CHANNEL_TO_PIN[c] for c in (prior_channels | {channel}))
        self.device.allocator.claim("awg", new_pins)
        try:
            self.device.backend.awg_upload_custom(channel=channel, samples=samples)
        except Exception:
            if prior_channels:
                prior_pins = sorted(_CHANNEL_TO_PIN[c] for c in prior_channels)
                self.device.allocator.claim("awg", prior_pins)
            else:
                self.device.allocator.release("awg")
            raise
        self._configured_channels.add(channel)
        self._amplitude[channel] = amplitude_v
        return {"uploaded": True, "channel": channel, "n_samples": len(samples), "pin": pin}

    def start(self, channel: int) -> dict[str, Any]:
        self.device.validate_channel(channel, "awg")
        if channel not in self._configured_channels:
            raise InstrumentNotConfigured(
                f"awg.configure or awg.upload_custom must be called for channel {channel} before start"
            )
        self.device.gate_output("awg_start", channel=channel, amplitude=self._amplitude.get(channel, 0.0))
        self.device.backend.awg_start(channel=channel)
        self._running_channels.add(channel)
        return {"started": True, "channel": channel}

    def stop(self, channel: int) -> dict[str, Any]:
        self.device.validate_channel(channel, "awg")
        self.device.backend.awg_stop(channel=channel)
        self._running_channels.discard(channel)
        return {"stopped": True, "channel": channel}

    def release(self) -> None:
        for ch in list(self._configured_channels):
            with contextlib.suppress(Exception):
                self.device.backend.awg_stop(channel=ch)
        self.device.allocator.release("awg")
        self._configured_channels.clear()
        self._amplitude.clear()
        self._running_channels.clear()
