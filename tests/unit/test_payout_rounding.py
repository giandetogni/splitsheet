"""Unit tests for largest-remainder cent allocation. Pure Decimal, no GCP, no floats.

The fixtures are built to BREAK naive rounding on purpose:

  * thirds of a dollar, where independent rounding loses a cent;
  * shares that produce identical discarded fractions, so the tiebreak has to decide;
  * a gross whose cent value is itself a half-cent boundary;
  * a single holder, four holders, and shares just above the minimum.

What is asserted every time is the invariant that matters on a financial statement:
SUM(published) == published gross, exactly, in cents.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from payout.rounding import (
    allocate,
    publish_gross,
    sql_floor_cents,
    sql_remainder_fraction,
    sql_remainder_rank,
)

THIRDS = [("MRH-000001", Decimal("33.3333")),
          ("MRH-000002", Decimal("33.3333")),
          ("MRH-000003", Decimal("33.3334"))]

EQUAL_HALVES = [("MRH-000002", Decimal("50.0000")), ("MRH-000001", Decimal("50.0000"))]

FOUR_WAY = [("MRH-000010", Decimal("25.0000")), ("MRH-000011", Decimal("25.0000")),
            ("MRH-000012", Decimal("25.0000")), ("MRH-000013", Decimal("25.0000"))]


def total(allocations) -> Decimal:
    return sum(a.published for a in allocations)


# --- the closure invariant ----------------------------------------------------------------

@pytest.mark.parametrize("gross", [
    "0.00", "0.01", "0.03", "1.00", "1.01", "9.99", "10.00", "0.07",
    "123.45", "0.005", "0.015", "2.995", "1000.00", "0.0349",
])
@pytest.mark.parametrize("holders", [THIRDS, EQUAL_HALVES, FOUR_WAY])
def test_published_amounts_always_close_to_the_published_gross(gross, holders):
    g = Decimal(gross)
    allocations = allocate(g, holders)
    assert total(allocations) == publish_gross(g), (gross, holders)


def test_thirds_of_a_dollar_do_not_lose_a_cent():
    """The canonical failure: 0.33 + 0.33 + 0.33 = 0.99. Largest remainder makes it 1.00."""
    allocations = allocate(Decimal("1.00"), THIRDS)
    assert total(allocations) == Decimal("1.00")
    assert sorted(str(a.published) for a in allocations) == ["0.33", "0.33", "0.34"]
    # Naive independent rounding would have produced 0.99; assert the gap is really closed.
    assert total(allocations) - Decimal("0.99") == Decimal("0.01")


def test_a_single_holder_receives_the_entire_published_gross():
    allocations = allocate(Decimal("1.239"), [("MRH-000001", Decimal("100.0000"))])
    assert len(allocations) == 1
    assert allocations[0].published == publish_gross(Decimal("1.239")) == Decimal("1.24")


def test_no_holder_receives_more_than_one_remainder_cent():
    for gross in ("0.01", "0.02", "0.07", "1.00", "1.01", "5.555"):
        allocations = allocate(Decimal(gross), FOUR_WAY)
        for a in allocations:
            assert a.published - Decimal(a.floor_cents) * Decimal("0.01") <= Decimal("0.01")
        given = sum(1 for a in allocations if a.got_remainder_cent)
        assert given <= len(FOUR_WAY)


def test_amounts_are_never_negative():
    for gross in ("0.00", "0.01", "3.33"):
        assert all(a.published >= 0 for a in allocate(Decimal(gross), THIRDS))


def test_negative_gross_is_refused_rather_than_distributed():
    with pytest.raises(ValueError, match="negative gross"):
        allocate(Decimal("-1.00"), THIRDS)


def test_negative_share_is_refused():
    with pytest.raises(ValueError, match="negative share"):
        allocate(Decimal("1.00"), [("MRH-000001", Decimal(-50)),
                                   ("MRH-000002", Decimal(150))])


# --- fractional cents, built deliberately -------------------------------------------------

def test_a_gross_that_cannot_be_split_evenly_still_closes():
    """0.07 across three holders: 2.3333 cents each. Two holders get 2c, one gets 3c."""
    allocations = allocate(Decimal("0.07"), THIRDS)
    assert total(allocations) == Decimal("0.07")
    assert sorted(str(a.published) for a in allocations) == ["0.02", "0.02", "0.03"]


def test_every_holder_can_receive_a_cent_when_the_remainder_is_large():
    """0.03 across four equal holders: each floors to 0, so all four remainder cents are needed
    minus one -- three holders get a cent and the total is exactly 0.03."""
    allocations = allocate(Decimal("0.03"), FOUR_WAY)
    assert total(allocations) == Decimal("0.03")
    assert sum(1 for a in allocations if a.got_remainder_cent) == 3


def test_internal_precision_is_kept_before_rounding():
    """holder_unrounded must retain sub-cent precision; rounding happens once, at the end."""
    allocations = allocate(Decimal("1.00"), THIRDS)
    assert any(a.unrounded != a.unrounded.quantize(Decimal("0.01")) for a in allocations)
    assert allocations[0].unrounded == Decimal("1.00") * Decimal("33.3333") / Decimal(100)


def test_a_half_cent_gross_rounds_once_and_the_parts_follow():
    """Half-up at the single rounding point: 2.995 publishes as 3.00 and the parts sum to it."""
    g = Decimal("2.995")
    assert publish_gross(g) == Decimal("3.00")
    assert total(allocate(g, THIRDS)) == Decimal("3.00")


# --- the deterministic tiebreak -----------------------------------------------------------

def test_identical_fractions_are_broken_by_holder_id_ascending():
    """Two holders, one cent to give, identical discarded fractions. The lower id must win, and it
    must win regardless of the order the holders arrive in."""
    forward = allocate(Decimal("0.01"), EQUAL_HALVES)
    backward = allocate(Decimal("0.01"), list(reversed(EQUAL_HALVES)))
    winner_f = next(a.rights_holder_id for a in forward if a.got_remainder_cent)
    winner_b = next(a.rights_holder_id for a in backward if a.got_remainder_cent)
    assert winner_f == winner_b == "MRH-000001"
    fractions = {a.remainder_fraction for a in forward}
    assert len(fractions) == 1, "the fixture is supposed to produce a tie"


def test_allocation_is_independent_of_input_order():
    for gross in ("0.01", "0.03", "1.00", "7.77"):
        a = {x.rights_holder_id: x.published for x in allocate(Decimal(gross), FOUR_WAY)}
        b = {x.rights_holder_id: x.published
             for x in allocate(Decimal(gross), list(reversed(FOUR_WAY)))}
        assert a == b, gross


def test_repeated_allocation_is_identical():
    first = allocate(Decimal("1.00"), THIRDS)
    second = allocate(Decimal("1.00"), THIRDS)
    assert [(a.rights_holder_id, a.published) for a in first] == \
           [(a.rights_holder_id, a.published) for a in second]


def test_larger_fraction_beats_the_id_tiebreak():
    """The tiebreak only applies to ties: a bigger discarded fraction must win outright even when
    its holder id sorts last."""
    holders = [("MRH-000009", Decimal("60.0000")), ("MRH-000001", Decimal("40.0000"))]
    allocations = allocate(Decimal("0.01"), holders)
    winner = next(a.rights_holder_id for a in allocations if a.got_remainder_cent)
    assert winner == "MRH-000009"


# --- everything is Decimal ----------------------------------------------------------------

def test_no_value_in_an_allocation_is_a_float():
    for a in allocate(Decimal("1.00"), THIRDS):
        for value in (a.share_pct, a.unrounded, a.remainder_fraction, a.published):
            assert isinstance(value, Decimal), a
        assert isinstance(a.floor_cents, int)


# --- the generated SQL matches the Python -------------------------------------------------

def test_sql_helpers_use_numeric_literals_and_the_documented_order():
    assert "NUMERIC '0.01'" in sql_floor_cents("x")
    assert "NUMERIC '0.01'" in sql_remainder_fraction("x")
    assert "FLOAT64" not in sql_floor_cents("x")
    rank = sql_remainder_rank("recording_mbid")
    assert "remainder_fraction DESC" in rank
    assert "rights_holder_id ASC" in rank
