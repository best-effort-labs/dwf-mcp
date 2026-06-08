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


class Decoder(ABC):
    protocol_name: ClassVar[str]

    @abstractmethod
    def decode(
        self,
        samples: np.ndarray,      # (n_samples, 16) uint8
        pin_map: dict[str, int],  # signal name → column index
        **config: Any,
    ) -> list[Any]:
        ...
