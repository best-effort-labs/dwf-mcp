"""PinInventory: the resolved pin namespace for an open device, combining queried
counts (DeviceInfo) with the non-queryable topology (DeviceProfile)."""
from __future__ import annotations

from dataclasses import dataclass

from dwf_mcp.backend import DeviceInfo
from dwf_mcp.devices.profiles import DeviceProfile, PinBank

# Virtual (non-physical) resources the allocator arbitrates, independent of device.
_VIRTUAL_RESOURCES = ["digital_in", "i2c_engine", "spi_engine", "uart_engine", "can_engine"]


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
        supply_pins=["vpos", "vneg"],
        trigger_pins=["trig1", "trig2"],
        virtual_resources=list(_VIRTUAL_RESOURCES),
        input_only=frozenset(input_only),
        _bank_of=bank_of,
    )
