"""PinInventory: the resolved pin namespace for an open device, combining queried
counts (DeviceInfo) with the non-queryable topology (DeviceProfile)."""
from __future__ import annotations

from dataclasses import dataclass

from dwf_mcp.backend import DeviceInfo
from dwf_mcp.devices.profiles import DeviceProfile

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

    def all_physical_pins(self) -> list[str]:
        return [*self.dio_pins, *self.scope_pins, *self.awg_pins,
                *self.supply_pins, *self.trigger_pins]

    def all_known(self) -> set[str]:
        return set(self.all_physical_pins()) | set(self.virtual_resources)

    def is_valid_pin(self, pin: str) -> bool:
        return pin in self.all_known()


def build_inventory(profile: DeviceProfile, info: DeviceInfo) -> PinInventory:
    return PinInventory(
        dio_pins=[f"dio{i}" for i in range(info.dio_count)],
        scope_pins=[f"scope{i}" for i in range(1, info.analog_in_channels + 1)],
        awg_pins=[f"awg{i}" for i in range(1, profile.user_awg_count + 1)],
        supply_pins=["vpos", "vneg"],
        trigger_pins=["trig1", "trig2"],
        virtual_resources=list(_VIRTUAL_RESOURCES),
    )
