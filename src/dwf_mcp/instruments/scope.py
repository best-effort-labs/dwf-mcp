"""Scope (analog-in) instrument. Buffer-mode and streaming record-mode acquisition."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.streaming import RecordingSession, notification_loop, process_chunk, record_loop

log = logging.getLogger(__name__)

_VALID_COUPLINGS = {"DC", "AC"}
_VALID_CONDITIONS = {"Rising", "Falling", "Either"}

SCOPE_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channels", "range_v", "sample_rate_hz", "buffer_size"],
    "properties": {
        "channels": {
            "type": "array",
            "items": {"type": "integer", "enum": [1, 2]},
            "minItems": 1,
            "uniqueItems": True,
        },
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0},
        "offset_v": {"type": "number", "default": 0.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "buffer_size": {"type": "integer", "minimum": 16, "maximum": 1_048_576},
    },
}

SCOPE_TRIGGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source"],
    "properties": {
        "source": {
            "type": "string",
            "enum": ["none", "detector_analog_in", "external1", "external2"],
        },
        "channel": {"type": "integer", "enum": [1, 2]},
        "level_v": {"type": "number", "default": 0.0},
        "condition": {
            "type": "string",
            "enum": ["Rising", "Falling", "Either"],
            "default": "Rising",
        },
        "position_s": {"type": "number", "default": 0.0},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}

SCOPE_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_path": {"type": "string"},
        "description": {"type": "string"},
    },
}

SCOPE_RECORD_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channels", "range_v", "sample_rate_hz", "duration_s"],
    "properties": {
        "channels": {
            "type": "array",
            "items": {"type": "integer", "enum": [1, 2]},
            "minItems": 1,
            "uniqueItems": True,
        },
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0},
        "offset_v": {"type": "number", "default": 0.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "duration_s": {"type": "number", "minimum": 0.001},
        "output_path": {"type": "string"},
    },
}

SCOPE_RECORD_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["record_id"],
    "properties": {"record_id": {"type": "string"}},
}


class Scope(Instrument):
    name = "scope"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":      ("configure",      SCOPE_CONFIGURE_SCHEMA),
        "set_trigger":    ("set_trigger",    SCOPE_TRIGGER_SCHEMA),
        "capture":        ("capture",        SCOPE_CAPTURE_SCHEMA),
        "record_start":   ("record_start",   SCOPE_RECORD_START_SCHEMA),
        "record_status":  ("record_status",  SCOPE_RECORD_ID_SCHEMA),
        "record_stop":    ("record_stop",    SCOPE_RECORD_ID_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None
        self._trigger: dict[str, Any] | None = None
        self._mode: Literal[None, "buffer", "record"] = None
        self._sessions: dict[str, RecordingSession] = {}

    # --- Buffer-mode ---

    def configure(
        self,
        channels: list[int],
        range_v: float,
        sample_rate_hz: float,
        buffer_size: int,
        offset_v: float = 0.0,
        coupling: str = "DC",
    ) -> dict[str, Any]:
        if self._mode == "record":
            raise RuntimeError(
                "scope is in record mode — call scope.record_stop() before reconfiguring"
            )
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(
                f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}"
            )
        pin_names = [f"scope{c}" for c in channels]
        self.device.allocator.claim("scope", pin_names)
        self._config = None
        self._trigger = None
        try:
            for ch in (1, 2):
                self.device.backend.scope_configure(
                    channel=ch,
                    range_v=range_v,
                    offset_v=offset_v,
                    coupling=coupling,
                    enable=(ch in channels),
                )
            self.device.backend.scope_set_acquisition(
                sample_rate_hz=sample_rate_hz,
                buffer_size=buffer_size,
                mode="Single",
            )
        except Exception:
            self.device.allocator.release("scope")
            raise
        self._config = {
            "channels": list(channels),
            "range_v": range_v,
            "offset_v": offset_v,
            "coupling": coupling,
            "sample_rate_hz": sample_rate_hz,
            "buffer_size": buffer_size,
        }
        self._mode = "buffer"
        return {"configured": True}

    def set_trigger(
        self,
        source: str,
        channel: int | None = None,
        level_v: float = 0.0,
        condition: str = "Rising",
        position_s: float = 0.0,
        timeout_s: float = 1.0,
    ) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured(
                "scope.configure must be called before set_trigger"
            )
        if condition not in _VALID_CONDITIONS:
            raise ValueError(
                f"condition must be one of {sorted(_VALID_CONDITIONS)}, got {condition!r}"
            )
        self.device.backend.scope_set_trigger(
            source=source,
            channel=channel,
            level_v=level_v,
            condition=condition,
            position_s=position_s,
            timeout_s=timeout_s,
        )
        self._trigger = {
            "source": source,
            "channel": channel,
            "level_v": level_v,
            "condition": condition,
            "position_s": position_s,
            "timeout_s": timeout_s,
        }
        return {"trigger_set": True}

    def capture(
        self,
        output_path: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        if self._config is None or self._mode != "buffer":
            raise InstrumentNotConfigured(
                "scope.configure must be called before capture"
            )
        cfg = self._config
        self.device.backend.scope_arm()
        deadline = time.monotonic() + max(
            cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0
        )
        while time.monotonic() < deadline:
            if self.device.backend.scope_status() == "Done":
                break
        else:
            raise RuntimeError("scope capture did not complete before deadline")

        arrays: dict[str, np.ndarray[Any, Any]] = {}
        summary_per_ch: dict[str, dict[str, float]] = {}
        for ch in cfg["channels"]:
            samples = self.device.backend.scope_read(
                channel=ch, count=cfg["buffer_size"]
            )
            arrays[f"ch{ch}"] = samples
            summary_per_ch[f"ch{ch}"] = self._summarize(samples, cfg["sample_rate_hz"])

        summary = CaptureSummary(
            instrument="scope",
            sample_count=cfg["buffer_size"],
            sample_rate_hz=cfg["sample_rate_hz"],
            extra=summary_per_ch,
        )
        sidecar_config = {**cfg, "trigger": self._trigger}
        result = self.artifacts.write_npz(
            instrument="scope",
            arrays=arrays,
            config=sidecar_config,
            summary=summary,
            output_path=Path(output_path) if output_path else None,
            description=description,
        )
        return {
            "path": result.path,
            "sidecar_path": result.sidecar_path,
            "summary": summary_per_ch,
        }

    # --- Record-mode ---

    async def record_start(
        self,
        channels: list[int],
        range_v: float,
        sample_rate_hz: float,
        duration_s: float,
        offset_v: float = 0.0,
        coupling: str = "DC",
        output_path: str | None = None,
        on_chunk: Callable[[str, np.ndarray], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if self._mode == "record":
            raise RuntimeError(
                "scope is already in record mode — call scope.record_stop() first"
            )
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(
                f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}"
            )
        # Implicit buffer release when switching from buffer to record mode.
        if self._mode == "buffer":
            self.device.allocator.release("scope")
            self._config = None
            self._trigger = None
            self._mode = None

        pin_names = [f"scope{c}" for c in channels]
        self.device.allocator.claim("scope", pin_names)
        try:
            self.device.backend.scope_record_configure(
                channels=list(channels),
                range_v=range_v,
                offset_v=offset_v,
                coupling=coupling,
                sample_rate_hz=sample_rate_hz,
                duration_s=duration_s,
            )
            self.device.backend.scope_record_arm()
        except Exception:
            self.device.allocator.release("scope")
            raise

        record_id = str(uuid.uuid4())
        queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue(maxsize=32)
        session = RecordingSession(
            record_id=record_id,
            task=None,
            notification_task=None,
            queue=queue,
            chunks=[],
            lost_samples=0,
            done=False,
            error=None,
            on_chunk=on_chunk,
            on_chunk_sync=None,
            meta={
                "channels": list(channels),
                "range_v": range_v,
                "sample_rate_hz": sample_rate_hz,
                "output_path": output_path,
            },
        )
        try:
            session.task = asyncio.create_task(
                record_loop(
                    session,
                    self.device.backend.scope_record_status,
                    self.device.backend.scope_record_read,
                )
            )
            if on_chunk is not None:
                session.notification_task = asyncio.create_task(
                    notification_loop(session, on_chunk)
                )
        except Exception:
            if session.task is not None:
                session.task.cancel()
            try:
                self.device.backend.scope_record_stop()
            except Exception:
                pass
            self.device.allocator.release("scope")
            raise

        self._sessions[record_id] = session
        self._mode = "record"
        return {"record_id": record_id}

    def record_status(self, record_id: str) -> dict[str, Any]:
        session = self._sessions.get(record_id)
        if session is None:
            raise ValueError(f"unknown record_id {record_id!r}")
        return {
            "record_id": record_id,
            "done": session.done,
            "chunks_received": len(session.chunks),
            "lost_samples": session.lost_samples,
            "error": session.error,
        }

    async def record_stop(self, record_id: str) -> dict[str, Any]:
        session = self._sessions.get(record_id)
        if session is None:
            raise ValueError(f"unknown record_id {record_id!r}")
        artifact_path: str | None = None
        sidecar_path: str | None = None
        artifact_error: str | None = None
        try:
            # 1. Cancel background tasks.
            if session.task is not None:
                session.task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.task
            if session.notification_task is not None:
                session.notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.notification_task
            # 2. Stop hardware.
            try:
                self.device.backend.scope_record_stop()
            except Exception as exc:
                log.warning("scope_record_stop failed: %s", exc)
            # 3. Drain remaining samples.
            try:
                available, lost, _ = self.device.backend.scope_record_status()
                session.lost_samples += lost
                if available > 0:
                    chunk = self.device.backend.scope_record_read(available)
                    try:
                        process_chunk(session, chunk)
                    except Exception as exc:
                        log.warning("drain process_chunk failed: %s", exc)
            except Exception as exc:
                log.warning("drain after scope_record_stop failed: %s", exc)
            # 4. Write npz artifact (best-effort).
            if session.chunks:
                try:
                    channels = session.meta["channels"]
                    all_raw = np.concatenate(session.chunks, axis=0)
                    # scope_record_read always returns shape (N, 2); slice to configured channels.
                    ch_indices = [c - 1 for c in channels]
                    arrays = {f"ch{c}": all_raw[:, i] for i, c in zip(ch_indices, channels)}
                    summary_per_ch = {
                        f"ch{c}": self._summarize(
                            all_raw[:, i], session.meta["sample_rate_hz"]
                        )
                        for i, c in zip(ch_indices, channels)
                    }
                    summary = CaptureSummary(
                        instrument="scope",
                        sample_count=all_raw.shape[0],
                        sample_rate_hz=session.meta["sample_rate_hz"],
                        extra=summary_per_ch,
                    )
                    out = session.meta.get("output_path")
                    npz_result = self.artifacts.write_npz(
                        instrument="scope",
                        arrays=arrays,
                        config=session.meta,
                        summary=summary,
                        output_path=Path(out) if out else None,
                    )
                    artifact_path = npz_result.path
                    sidecar_path = npz_result.sidecar_path
                except Exception as exc:
                    log.exception("artifact write failed for record_id=%r", record_id)
                    artifact_error = str(exc)
        finally:
            del self._sessions[record_id]
            self.device.allocator.release("scope")
            self._mode = None

        return {
            "record_id": record_id,
            "artifact_path": artifact_path,
            "sidecar_path": sidecar_path,
            "lost_samples": session.lost_samples,
            "error": session.error,
            "artifact_error": artifact_error,
        }

    def release(self) -> None:
        for session in list(self._sessions.values()):
            if session.task is not None:
                session.task.cancel()
            if session.notification_task is not None:
                session.notification_task.cancel()
        self._sessions.clear()
        self.device.allocator.release("scope")
        self._config = None
        self._trigger = None
        self._mode = None

    @staticmethod
    def _summarize(
        samples: np.ndarray[Any, Any], sample_rate_hz: float
    ) -> dict[str, float]:
        arr = np.asarray(samples, dtype=np.float64)
        if len(arr) == 0:
            return {
                "min": 0.0, "max": 0.0, "mean": 0.0, "rms": 0.0,
                "freq_estimate": 0.0, "sample_rate": sample_rate_hz,
            }
        mean = float(arr.mean())
        rms = float(np.sqrt(np.mean(arr**2)))
        if len(arr) > 0:
            midpoint = float(arr.max() + arr.min()) / 2.0
            centered = arr - midpoint
            signs = np.signbit(centered)
            crossings = int(np.sum(signs[:-1] != signs[1:]))
            freq_estimate = (crossings / 2.0) * (sample_rate_hz / len(arr))
        else:
            freq_estimate = 0.0
        return {
            "min": float(arr.min()),
            "max": float(arr.max()),
            "mean": mean,
            "rms": rms,
            "freq_estimate": freq_estimate,
            "sample_rate": sample_rate_hz,
        }
