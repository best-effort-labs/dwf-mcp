from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.device import DwfDevice


class InstrumentNotConfigured(Exception):
    """Raised when a tool is called on an instrument that hasn't been configured."""


class Instrument(ABC):
    name: ClassVar[str]
    # MCP tool suffix -> (method_name_on_instance, input_schema_dict)
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]]

    @abstractmethod
    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None: ...

    @abstractmethod
    def release(self) -> None: ...
