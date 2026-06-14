from __future__ import annotations

from dataclasses import dataclass, field


class PinAllocationError(Exception):
    """Raised when an instrument tries to claim pins already in use, or a resource group conflict."""


@dataclass(frozen=True)
class ResourceGroup:
    name: str
    pins: frozenset[str]
    exclusive: bool = True

    def __init__(self, name: str, pins: set[str] | frozenset[str], exclusive: bool = True) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "pins", frozenset(pins))
        object.__setattr__(self, "exclusive", exclusive)


@dataclass
class PinAllocator:
    """Tracks which instrument owns which DIO pins / virtual resources.

    Not thread-safe: assumes single-threaded access (the MCP server runs one
    coroutine per tool call sequentially in a single asyncio event loop). If
    you ever move backend polling to a worker thread, add a Lock around the
    `claim`/`release`/`claim_observe` methods.
    """
    resource_groups: list[ResourceGroup] = field(default_factory=list)
    _claims: dict[str, list[str]] = field(default_factory=dict)
    _observe_claims: set[str] = field(default_factory=set)  # instruments with DigitalIn observer claim
    _known_pins: set[str] | None = field(default=None)  # None = unconfigured (permissive)

    def configure(self, known_pins: set[str], resource_groups: list[ResourceGroup]) -> None:
        self._known_pins = set(known_pins)
        self.resource_groups = list(resource_groups)

    def reset_configuration(self) -> None:
        self._known_pins = None
        self.resource_groups = []
        self.clear()

    def claim(self, instrument: str, pins: list[str]) -> None:
        if self._known_pins is not None:
            unknown = [p for p in pins if p not in self._known_pins]
            if unknown:
                raise PinAllocationError(
                    f"{instrument} cannot claim unknown pin(s) {unknown}; "
                    f"not present on this device"
                )
        # Replacement semantics: re-claiming for the same instrument logically
        # releases its old pins. Validate against the tentative post-release view
        # FIRST so a failed re-claim leaves the existing claim untouched (the
        # caller's hardware state remains coherent with the allocator's record).
        tentative_claims = {k: v for k, v in self._claims.items() if k != instrument}
        tentative_pin_owners = {
            pin: instr for instr, claim_pins in tentative_claims.items() for pin in claim_pins
        }
        for pin in pins:
            if pin in tentative_pin_owners:
                raise PinAllocationError(
                    f"{instrument} cannot claim {pin}: already held by {tentative_pin_owners[pin]}"
                )
        # "digital_in" virtual pin conflicts with any existing observer claim.
        if "digital_in" in pins and self._observe_claims:
            observers = ", ".join(sorted(self._observe_claims))
            raise PinAllocationError(
                f"{instrument} cannot claim DigitalIn: already held by observer(s) ({observers})"
            )
        for group in self.resource_groups:
            if not group.exclusive:
                continue
            requested_in_group = group.pins & set(pins)
            if not requested_in_group:
                continue
            for other_instr, other_pins in tentative_claims.items():
                if set(other_pins) & group.pins:
                    raise PinAllocationError(
                        f"{instrument} cannot claim {sorted(requested_in_group)}: "
                        f"resource group {group.name!r} is held by {other_instr}"
                    )
        # All checks passed — commit atomically.
        self._claims[instrument] = list(pins)

    def claim_observe(self, instrument: str) -> None:
        """Reserve DigitalIn as a read-only observer.
        Does not conflict with DigitalOut writers on the same physical pins.
        Conflicts with any exclusive DigitalIn claim or another observer."""
        pin_owners = self.claimed_pins()
        if "digital_in" in pin_owners:
            raise PinAllocationError(
                f"{instrument} cannot observe DigitalIn: held exclusively by {pin_owners['digital_in']}"
            )
        if self._observe_claims:
            other = next(iter(self._observe_claims))
            raise PinAllocationError(
                f"{instrument} cannot observe DigitalIn: already observing ({other})"
            )
        self._observe_claims.add(instrument)

    def release(self, instrument: str) -> None:
        self._claims.pop(instrument, None)
        self._observe_claims.discard(instrument)

    def claimed_pins(self) -> dict[str, str]:
        return {pin: instr for instr, pins in self._claims.items() for pin in pins}

    def claimed_instruments(self) -> list[str]:
        return list(self._claims.keys())

    def clear(self) -> None:
        self._claims.clear()
        self._observe_claims.clear()
