from __future__ import annotations

from dwf_mcp.instrument import Instrument


class InstrumentRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[Instrument]] = {}

    def register(self, cls: type[Instrument]) -> None:
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not name:
            raise TypeError(f"{cls.__name__} must declare a non-empty `name` class attribute")
        if name in self._classes:
            raise ValueError(f"instrument {name!r} already registered")
        self._classes[name] = cls

    def get_class(self, name: str) -> type[Instrument]:
        try:
            return self._classes[name]
        except KeyError as exc:
            raise KeyError(f"unknown instrument {name!r}") from exc

    def names(self) -> list[str]:
        return list(self._classes.keys())
