"""PinInventory: the resolved pin namespace for an open device, combining queried
counts (DeviceInfo) with the non-queryable topology (DeviceProfile)."""
from __future__ import annotations

from dataclasses import dataclass

from dwf_mcp.backend import DeviceInfo
from dwf_mcp.devices.profiles import DeviceProfile, PinBank

# protocol instrument -> the virtual engine resource the allocator arbitrates
_ENGINE_RESOURCE = {
    "i2c": "i2c_engine", "spi": "spi_engine",
    "uart": "uart_engine", "can": "can_engine",
}


def _virtual_resources(supported: frozenset[str]) -> list[str]:
    """The virtual (non-physical) resources the allocator arbitrates, for a device
    with these supported instruments. `digital_in` = the DigitalIn block (logic
    analyzer + sniff observe); each protocol engine is present only if its
    instrument is supported."""
    res: list[str] = []
    if "logic" in supported:
        res.append("digital_in")
    res += [eng for instr, eng in _ENGINE_RESOURCE.items() if instr in supported]
    return res


@dataclass(frozen=True)
class PinInventory:
    dio_pins: list[str]
    scope_pins: list[str]
    awg_pins: list[str]
    supply_pins: list[str]
    trigger_pins: list[str]
    virtual_resources: list[str]
    input_only: frozenset[str]
    _bank_of: dict[str, PinBank]

    def all_physical_pins(self) -> list[str]:
        return [*self.dio_pins, *self.scope_pins, *self.awg_pins,
                *self.supply_pins, *self.trigger_pins]

    def all_known(self) -> set[str]:
        return set(self.all_physical_pins()) | set(self.virtual_resources)

    def is_valid_pin(self, pin: str) -> bool:
        return pin in self.all_known()

    def is_valid_physical_pin(self, pin: str) -> bool:
        return pin in set(self.all_physical_pins())

    def subsystem_bit(self, pin: str, subsystem: str) -> int:
        """Resolve a digital pin to its bit index for the target SDK subsystem.

        digitalio / digitalout: bank-relative (pin# - bank.start).
        digitalin: inputOrder / global layout (pin#).
        """
        num = int(pin[3:])
        if subsystem == "digitalin":
            return num
        if subsystem in ("digitalio", "digitalout"):
            bank = self._bank_of[pin]
            return num - bank.start
        raise ValueError(f"unknown subsystem {subsystem!r}")


def build_inventory(profile: DeviceProfile, info: DeviceInfo) -> PinInventory:
    if profile.pin_banks is not None:
        banks = profile.pin_banks
    else:
        banks = [PinBank("dio", 0, info.dio_count)]

    dio_pins: list[str] = []
    input_only: set[str] = set()
    bank_of: dict[str, PinBank] = {}

    for bank in banks:
        for n in range(bank.start, bank.start + bank.count):
            pin = f"{bank.prefix}{n}"
            dio_pins.append(pin)
            bank_of[pin] = bank
            if bank.input_only:
                input_only.add(pin)

    return PinInventory(
        dio_pins=dio_pins,
        scope_pins=[f"scope{i}" for i in range(1, info.analog_in_channels + 1)],
        awg_pins=[f"awg{i}" for i in range(1, profile.user_awg_count + 1)],
        supply_pins=["vpos", "vneg"] if "supply" in profile.supported_instruments else [],
        trigger_pins=[f"trig{i}" for i in range(1, profile.trigger_count + 1)],
        virtual_resources=_virtual_resources(profile.supported_instruments),
        input_only=frozenset(input_only),
        _bank_of=bank_of,
    )
