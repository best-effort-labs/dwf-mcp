from __future__ import annotations

import pytest

from dwf_mcp.allocator import PinAllocationError, PinAllocator, ResourceGroup


@pytest.fixture
def alloc() -> PinAllocator:
    return PinAllocator(resource_groups=[])


def test_claim_then_release_frees_pins(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    with pytest.raises(PinAllocationError):
        alloc.claim("uart", ["dio0", "dio2"])
    alloc.release("i2c")
    alloc.claim("uart", ["dio0", "dio2"])  # now OK


def test_claim_lists_claimed_pins(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    alloc.claim("uart", ["dio2", "dio3"])
    assert alloc.claimed_pins() == {
        "dio0": "i2c", "dio1": "i2c", "dio2": "uart", "dio3": "uart"
    }


def test_double_claim_by_same_instrument_is_replacement(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    alloc.claim("i2c", ["dio4", "dio5"])  # reconfigure
    assert alloc.claimed_pins() == {"dio4": "i2c", "dio5": "i2c"}


def test_resource_group_conflict() -> None:
    # Scope ch1 and ch2 are co-sampled: configuring one locks the other for the same instrument.
    groups = [ResourceGroup(name="scope_pair", pins={"scope1", "scope2"}, exclusive=True)]
    alloc = PinAllocator(resource_groups=groups)
    alloc.claim("scope", ["scope1"])
    # A different instrument cannot grab scope2 either, because the group is exclusive.
    with pytest.raises(PinAllocationError) as exc:
        alloc.claim("other", ["scope2"])
    assert "scope_pair" in str(exc.value)


def test_release_unknown_instrument_is_noop(alloc: PinAllocator) -> None:
    alloc.release("never_claimed")  # no raise


def test_clear_releases_everything(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    alloc.claim("uart", ["dio2", "dio3"])
    alloc.clear()
    assert alloc.claimed_pins() == {}
