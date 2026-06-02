from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class Instrument(ABC):
    name: ClassVar[str]
    required_pins: ClassVar[list[str]]

    @abstractmethod
    def configure(self, **kwargs: object) -> None: ...

    @abstractmethod
    def release(self) -> None: ...
