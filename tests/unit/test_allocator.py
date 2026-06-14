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


def test_failed_reclaim_preserves_prior_claim(alloc: PinAllocator) -> None:
    """A failed re-claim by the same instrument must NOT drop its old pins.
    Otherwise the allocator and hardware get out of sync on conflict."""
    alloc.claim("spi", ["dio0", "dio1"])
    alloc.claim("uart", ["dio4"])

    # spi tries to reconfigure to a pin owned by uart — should raise AND keep
    # its original [dio0, dio1] claim.
    with pytest.raises(PinAllocationError):
        alloc.claim("spi", ["dio4", "dio5"])

    assert alloc.claimed_pins() == {"dio0": "spi", "dio1": "spi", "dio4": "uart"}


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


# --- claim_observe tests ---

def test_claim_observe_succeeds_when_digital_in_free() -> None:
    alloc = PinAllocator()
    alloc.claim_observe("sniff_spi")
    assert "sniff_spi" in alloc._observe_claims


def test_claim_observe_blocked_when_digital_in_exclusively_claimed() -> None:
    alloc = PinAllocator()
    alloc.claim("logic", ["digital_in", "dio0"])
    with pytest.raises(PinAllocationError, match="cannot observe DigitalIn"):
        alloc.claim_observe("sniff_spi")


def test_exclusive_digital_in_blocked_when_observer_exists() -> None:
    alloc = PinAllocator()
    alloc.claim_observe("sniff_spi")
    with pytest.raises(PinAllocationError, match="cannot claim DigitalIn"):
        alloc.claim("logic", ["digital_in", "dio0"])


def test_second_observer_blocked() -> None:
    alloc = PinAllocator()
    alloc.claim_observe("sniff_spi_1")
    with pytest.raises(PinAllocationError, match="already observing"):
        alloc.claim_observe("sniff_spi_2")


def test_observe_does_not_conflict_with_write_claims_on_same_pins() -> None:
    """DigitalIn observer and DigitalOut writer on the same physical pins should NOT conflict."""
    alloc = PinAllocator()
    alloc.claim("spi", ["spi_engine", "dio0", "dio1"])
    # claim_observe should succeed even though spi holds dio0/dio1 as write claims
    alloc.claim_observe("sniff_spi")  # should not raise


def test_release_removes_observe_claim() -> None:
    alloc = PinAllocator()
    alloc.claim_observe("sniff_spi")
    alloc.release("sniff_spi")
    assert "sniff_spi" not in alloc._observe_claims
    # Can claim_observe again after release
    alloc.claim_observe("sniff_spi_2")


def test_clear_removes_observe_claims() -> None:
    alloc = PinAllocator()
    alloc.claim_observe("sniff_spi")
    alloc.clear()
    assert len(alloc._observe_claims) == 0


# --- Stage 5 coexistence invariants ---

def test_observe_coexists_with_engine_and_dio_claim() -> None:
    """The KEY Stage 5 invariant: an observer (e.g. sniff.i2c_start) MUST NOT
    block a concurrent protocol master (i2c.configure + i2c.scan) on the
    same wires. claim_observe and claim share no resources."""
    alloc = PinAllocator()
    alloc.claim_observe("sniff_i2c_X")
    # This MUST succeed — no PinAllocationError.
    alloc.claim("i2c_master", ["i2c_engine", "dio0", "dio1"])
    assert "i2c_master" in alloc.claimed_instruments()


def test_two_observers_conflict() -> None:
    """Only one observer at a time. Stage 5 sniff.{i2c,uart,can,spi}_start tools
    each claim_observe — they can't coexist."""
    alloc = PinAllocator()
    alloc.claim_observe("sniff_spi_X")
    with pytest.raises(PinAllocationError):
        alloc.claim_observe("sniff_i2c_Y")


def test_exclusive_digital_in_blocked_while_observer_active() -> None:
    """An observer holds DigitalIn read-only. An exclusive DigitalIn claim
    (e.g. logic.record_start) must wait."""
    alloc = PinAllocator()
    alloc.claim_observe("sniff_i2c_X")
    with pytest.raises(PinAllocationError):
        alloc.claim("logic", ["digital_in"])


# --- Task 5: configure / reset_configuration + unknown-pin rejection ---

def test_configure_sets_known_pins_and_groups() -> None:
    a = PinAllocator()
    a.configure(known_pins={"dio0", "dio1", "digital_in"},
                resource_groups=[ResourceGroup("g", {"dio0"}, exclusive=True)])
    a.claim("logic", ["dio0"])  # known → ok
    assert a.claimed_pins() == {"dio0": "logic"}


def test_claim_unknown_pin_rejected_when_configured() -> None:
    a = PinAllocator()
    a.configure(known_pins={"dio0"}, resource_groups=[])
    with pytest.raises(PinAllocationError, match="unknown pin"):
        a.claim("logic", ["dio99"])


def test_unconfigured_allocator_accepts_any_pin() -> None:
    # Back-compat: before configure() (no device open), don't enforce.
    a = PinAllocator()
    a.claim("logic", ["dio0"])  # no raise


def test_reset_configuration_clears_groups_and_known() -> None:
    a = PinAllocator()
    a.configure(known_pins={"dio0"}, resource_groups=[ResourceGroup("g", {"dio0"})])
    a.claim("logic", ["dio0"])
    a.reset_configuration()
    assert a.resource_groups == []
    assert a.claimed_pins() == {}
    a.claim("x", ["dioZZ"])  # unconfigured again → permissive
