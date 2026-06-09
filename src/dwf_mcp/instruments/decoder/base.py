from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np


@dataclass
class SpiTransaction:
    timestamp_s: float
    word_index: int
    mosi: bytes
    miso: bytes | None       # None if no MISO pin captured
    cs_active: bool
    cs_error: bool           # CS deasserted mid-word
    error: bool = False
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "word_index": self.word_index,
            "mosi": self.mosi,
            "miso": self.miso,
            "cs_active": self.cs_active,
            "cs_error": self.cs_error,
            "error": self.error,
            "error_detail": self.error_detail,
        }


@dataclass
class UartFrame:
    timestamp_s: float
    data: bytes              # decoded byte (one byte per frame)
    parity_error: bool
    framing_error: bool
    # Break-condition detection (sustained start-level for longer than one
    # frame time) is not performed by UartDecoder; this field defaults to
    # None so the parquet row shape matches engine-mode sniff.uart records.
    break_condition: bool | None = None
    error: bool = False
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "data": self.data,
            "parity_error": self.parity_error,
            "framing_error": self.framing_error,
            "break_condition": self.break_condition,
            "error": self.error,
            "error_detail": self.error_detail,
        }


@dataclass
class CanFrame:
    timestamp_s: float
    frame_id: int
    extended: bool
    rtr: bool
    dlc: int
    data: bytes
    crc_valid: bool | None
    ack_received: bool | None
    error_type: str | None
    error: bool
    error_detail: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "frame_id": self.frame_id,
            "extended": self.extended,
            "rtr": self.rtr,
            "dlc": self.dlc,
            "data": self.data,
            "crc_valid": self.crc_valid,
            "ack_received": self.ack_received,
            "error_type": self.error_type,
            "error": self.error,
            "error_detail": self.error_detail,
        }


@dataclass
class I2cTransaction:
    timestamp_s: float
    type: str                # "read" or "write"
    address: int             # 7-bit address (or 10-bit when address_bits == 10)
    address_bits: int        # 7 or 10
    data: bytes              # payload bytes following the address byte(s)
    nak_at_byte: int | None  # None if all ACKed; 0 == NAK on address byte
    error: bool = False
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "type": self.type,
            "address": self.address,
            "address_bits": self.address_bits,
            "data": self.data,
            "nak_at_byte": self.nak_at_byte,
            "error": self.error,
            "error_detail": self.error_detail,
        }


class Decoder(ABC):
    """Protocol decoder. Stateful — supports either one-shot ``decode(samples)``
    or streaming via ``init/feed/finalize``.

    Subclasses MUST implement ``init`` (set up state), ``feed`` (process a
    chunk, return any transactions completed by it), and ``finalize`` (flush
    pending state and return any transactions still open).

    The default ``decode`` is a convenience wrapper that runs the full
    init→feed→finalize cycle on a single sample array.
    """

    protocol_name: ClassVar[str]

    @abstractmethod
    def init(
        self,
        pin_map: dict[str, int],
        sample_rate_hz: float,
        **config: Any,
    ) -> None:
        """Initialize per-stream state. Called once before any ``feed`` call.

        Raises ``ValueError`` if config is malformed (e.g. non-positive sample
        rate, insufficient oversampling).
        """
        ...

    @abstractmethod
    def feed(self, samples: np.ndarray) -> list[Any]:
        """Process the next chunk of samples and return any transactions/frames
        that completed within this chunk. Subsequent calls continue from where
        the previous one left off."""
        ...

    @abstractmethod
    def finalize(self) -> list[Any]:
        """Flush any pending state and return transactions that were still in
        progress at end-of-stream. Called once after the last ``feed``."""
        ...

    def decode(
        self,
        samples: np.ndarray,      # (n_samples, 16) uint8
        pin_map: dict[str, int],  # signal name → column index
        sample_rate_hz: float,
        **config: Any,
    ) -> list[Any]:
        """One-shot convenience wrapper around init / feed / finalize."""
        self.init(pin_map, sample_rate_hz=sample_rate_hz, **config)
        out = self.feed(samples)
        out.extend(self.finalize())
        return out
