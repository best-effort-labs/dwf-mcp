"""Device profiles: the non-queryable, device-keyed facts (resource-group
topology, pin naming, supported instruments, DIO voltage). Capability *values*
(channel counts, buffer sizes, rates) are queried at open, not stored here."""
from __future__ import annotations

from dataclasses import dataclass

from dwf_mcp.allocator import ResourceGroup

_ALL_INSTRUMENTS = frozenset({
    "scope", "awg", "supply", "logic", "pattern", "dio", "dmm",
    "i2c", "spi", "uart", "can", "sniff", "decoder",
})


class UnsupportedDeviceError(Exception):
    """Raised when an opened device's devid has no registered profile."""


@dataclass(frozen=True)
class DeviceProfile:
    devid: int
    name: str
    user_awg_count: int
    supported_instruments: frozenset[str]
    dio_voltage_options: list[float]

    def build_resource_groups(
        self, analog_in_channels: int, user_awg_count: int
    ) -> list[ResourceGroup]:
        scope_pins = {f"scope{i}" for i in range(1, analog_in_channels + 1)}
        awg_pins = {f"awg{i}" for i in range(1, user_awg_count + 1)}
        return [
            # All AnalogIn channels are co-sampled (non-exclusive: scope owns the
            # pair, dmm can take whichever the scope isn't using).
            ResourceGroup(name="scope_pair", pins=scope_pins, exclusive=False),
            # User AWG channels share a clock domain (exclusive).
            ResourceGroup(name="awg_clock", pins=awg_pins, exclusive=True),
        ]


def _classic(devid: int, name: str) -> DeviceProfile:
    """The classic Analog Discovery topology (AD1/AD2/AD3): 2 user AWG channels,
    all instruments, fixed 3.3 V DIO. They differ only by devid + name."""
    return DeviceProfile(
        devid=devid,
        name=name,
        user_awg_count=2,
        supported_instruments=_ALL_INSTRUMENTS,
        dio_voltage_options=[3.3],
    )


PROFILE_REGISTRY: dict[int, DeviceProfile] = {
    2: _classic(2, "Analog Discovery"),
    3: _classic(3, "Analog Discovery 2"),
    10: _classic(10, "Analog Discovery 3"),
}


def resolve_profile(devid: int) -> DeviceProfile:
    try:
        return PROFILE_REGISTRY[devid]
    except KeyError:
        raise UnsupportedDeviceError(
            f"unsupported Digilent device (devid {devid}); "
            f"add a profile to PROFILE_REGISTRY"
        ) from None
