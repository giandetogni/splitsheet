"""Unit tests for the MODELED rights generator. Pure: no GCP, no cost.

What is being defended:

  * determinism -- same seed, same rows, same ids, independent of order;
  * shares that sum to EXACTLY 100, in Decimal, never float;
  * half-open interval semantics at the boundary, which is the whole temporal model;
  * defect quotas that are exact and disjoint, so the quality report has an expected number to
    be compared against;
  * the modeled declaration travelling with the data, not just with the documentation;
  * config versioning proven by mutation.
"""

from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal

import pytest
import yaml

from rights.generator import (
    DEFAULT_CONFIG_PATH,
    DEFECT_ORDER,
    compute_digest,
    covers,
    holder_id,
    holder_row,
    holders_for,
    load_rights_model,
    ownership_rows,
    rate_card_rows,
    rate_gap_days,
    select_defects,
    shares_for,
    split_version_id,
    sql_covers,
    unit,
)

MODEL = load_rights_model()

#: Pinned. The generated rights data is reproducible only if this model is.
EXPECTED_RIGHTS_VERSION = "1.0.0+47f801102e17"

MBIDS = [f"00000000-0000-4000-8000-{i:012d}" for i in range(3000)]


def _raw() -> dict:
    with open(DEFAULT_CONFIG_PATH) as fh:
        return yaml.safe_load(fh)


def _write(tmp_path, raw: dict):
    p = tmp_path / "changed.yml"
    with open(p, "w") as fh:
        yaml.safe_dump(raw, fh)
    return p


# --- versioning, proven by mutation -------------------------------------------------------


def test_rights_version_is_pinned():
    assert MODEL.version == EXPECTED_RIGHTS_VERSION, (
        "the rights model changed without updating EXPECTED_RIGHTS_VERSION. Regenerating under "
        f"a new model is fine, but set it to {MODEL.version!r} in the same commit."
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.update({"seed": "different-seed"}), id="seed"),
        pytest.param(lambda r: r["holders"].update({"count": 70000}), id="holder_count"),
        pytest.param(lambda r: r["defects"].update({"temporal_gap": 401}), id="defect_quota"),
        pytest.param(
            lambda r: r["ownership"].update({"base_valid_from": "2024-01-01"}), id="interval"
        ),
        pytest.param(
            lambda r: r["rate_card"]["intervals"][1].update({"rate_per_stream": "0.0099000000"}),
            id="rate",
        ),
    ],
)
def test_changing_the_model_without_the_digest_fails_the_load(tmp_path, mutate):
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises(ValueError, match="digest"):
        load_rights_model(_write(tmp_path, raw))


def test_prose_edits_do_not_change_the_digest(tmp_path):
    raw = copy.deepcopy(_raw())
    raw["universe"]["scope"] = "reworded"
    assert compute_digest(raw) == MODEL.rules_digest
    load_rights_model(_write(tmp_path, raw))


def test_a_rate_card_gap_that_disagrees_with_the_config_is_rejected(tmp_path):
    """The expected gap is asserted against the intervals, so the two cannot drift."""
    raw = copy.deepcopy(_raw())
    raw["rate_card"]["expected_gap_days"] = 5
    raw["rules_sha256"] = compute_digest(raw)
    with pytest.raises(ValueError, match="uncovered days"):
        load_rights_model(_write(tmp_path, raw))


# --- determinism ---------------------------------------------------------------------------


def test_unit_is_in_range_and_deterministic():
    values = [unit(MODEL.seed, "kind", i) for i in range(500)]
    assert all(0.0 <= v < 1.0 for v in values)
    assert values == [unit(MODEL.seed, "kind", i) for i in range(500)]


def test_ownership_is_independent_of_generation_order():
    forward = {m: ownership_rows(MODEL, m) for m in MBIDS[:400]}
    backward = {m: ownership_rows(MODEL, m) for m in reversed(MBIDS[:400])}
    assert forward == backward


def test_split_version_id_is_stable_and_distinguishes_intervals():
    a = split_version_id(MODEL, MBIDS[0], MODEL.base_valid_from)
    b = split_version_id(MODEL, MBIDS[0], MODEL.change_date)
    assert a == split_version_id(MODEL, MBIDS[0], MODEL.base_valid_from)
    assert a != b, "an ownership change must produce a new split set, not a mutated one"
    assert a.startswith(MODEL.split_version_id_prefix)


def test_holder_ids_are_zero_padded_and_stable():
    assert holder_id(MODEL, 42) == "MRH-000042"
    assert holder_row(MODEL, 42) == holder_row(MODEL, 42)


# --- the modeled declaration travels with the data ----------------------------------------


def test_every_generated_row_declares_itself_modeled():
    assert holder_row(MODEL, 7)["is_modeled"] is True
    assert all(r["is_modeled"] is True for r in rate_card_rows(MODEL))


def test_holder_names_cannot_collide_with_a_real_organisation():
    for i in (0, 1, 999, 59_999):
        assert holder_row(MODEL, i)["display_name"] == f"Modeled Rights Holder {i:06d}"


# --- shares --------------------------------------------------------------------------------


def test_shares_are_decimal_not_float():
    for share in shares_for(MODEL, MBIDS[0], 4):
        assert isinstance(share, Decimal)


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_shares_sum_to_exactly_one_hundred(count):
    for mbid in MBIDS[:300]:
        shares = shares_for(MODEL, mbid, count)
        assert len(shares) == count
        assert sum(shares) == Decimal("100.0000"), (mbid, count, shares)


def test_no_share_is_below_the_configured_minimum():
    for mbid in MBIDS[:300]:
        for count in (1, 2, 3, 4):
            assert all(s >= MODEL.minimum_share_pct for s in shares_for(MODEL, mbid, count))


def test_share_precision_matches_the_configured_scale():
    for share in shares_for(MODEL, MBIDS[5], 3):
        assert -share.as_tuple().exponent <= MODEL.share_decimal_places


def test_holders_within_a_split_set_are_distinct():
    for mbid in MBIDS[:200]:
        for count in (1, 2, 3, 4):
            chosen = holders_for(MODEL, mbid, count)
            assert len(set(chosen)) == count


def test_assignment_never_touches_the_reserved_holder_block():
    """The reserved tail is what makes "exactly 1,000 holders have no split" true."""
    for mbid in MBIDS[:500]:
        for index in holders_for(MODEL, mbid, 4):
            assert index < MODEL.assignable_holders


def test_assignment_is_skewed_towards_low_ranks():
    drawn = [i for m in MBIDS for i in holders_for(MODEL, m, 1)]
    first_eighth = sum(1 for i in drawn if i < MODEL.assignable_holders / 8)
    assert (
        first_eighth / len(drawn) > 0.35
    ), "assignment_power is supposed to concentrate ownership; it does not"


# --- half-open intervals -------------------------------------------------------------------


def test_covers_includes_valid_from_and_excludes_valid_to():
    lo, hi = dt.date(2026, 6, 1), dt.date(2026, 6, 15)
    assert covers(lo, hi, lo)
    assert covers(lo, hi, hi - dt.timedelta(days=1))
    assert not covers(lo, hi, hi)
    assert not covers(lo, hi, lo - dt.timedelta(days=1))


def test_an_invalid_interval_covers_nothing():
    """valid_to <= valid_from selects no rows rather than raising, which is why the defect has
    to be detected by a check instead of by a crash."""
    lo, hi = MODEL.invalid_from, MODEL.invalid_to
    assert hi <= lo
    for day in (hi, lo, dt.date(2026, 6, 10)):
        assert not covers(lo, hi, day)


def test_sql_and_python_interval_rules_are_generated_from_one_definition():
    sql = sql_covers("valid_from", "valid_to", "listen_date")
    assert ">= valid_from" in sql
    assert "< valid_to" in sql
    assert "<=" not in sql.replace(">= valid_from", ""), "upper bound must be exclusive"


def test_mid_period_change_produces_contiguous_intervals_with_different_holders():
    changed = [m for m in MBIDS if len({r["valid_from"] for r in ownership_rows(MODEL, m)}) == 2]
    assert changed, "no recording in the sample received a mid-period ownership change"
    for mbid in changed[:20]:
        rows = ownership_rows(MODEL, mbid)
        before = {r["rights_holder_id"] for r in rows if r["valid_from"] == MODEL.base_valid_from}
        after = {r["rights_holder_id"] for r in rows if r["valid_from"] == MODEL.change_date}
        assert before and after
        assert not (before & after), (
            "the holder set must actually change, or a wrong interval predicate would still "
            "select a plausible holder"
        )
        # contiguous: the first interval ends exactly where the second begins
        ends = {r["valid_to"] for r in rows if r["valid_from"] == MODEL.base_valid_from}
        assert ends == {MODEL.change_date}
        assert sum(r["share_pct"] for r in rows if r["valid_from"] == MODEL.change_date) == Decimal(
            "100.0000"
        )


# --- deliberate defects --------------------------------------------------------------------


def test_defect_quotas_are_exact_and_disjoint():
    selected = select_defects(MODEL, MBIDS)
    for kind in DEFECT_ORDER:
        got = sum(1 for v in selected.values() if v == kind)
        assert got == MODEL.defect_counts[kind], kind
    assert len(selected) == sum(MODEL.defect_counts[k] for k in DEFECT_ORDER)


def test_defect_selection_does_not_depend_on_input_order():
    assert select_defects(MODEL, MBIDS) == select_defects(MODEL, list(reversed(MBIDS)))


def test_sum_defect_really_breaks_the_sum():
    rows = ownership_rows(MODEL, MBIDS[0], "shares_do_not_sum_to_100")
    assert sum(r["share_pct"] for r in rows) != Decimal("100.0000")


def test_overlap_defect_really_overlaps_inside_june():
    rows = ownership_rows(MODEL, MBIDS[0], "temporal_overlap")
    spans = sorted({(r["valid_from"], r["valid_to"]) for r in rows})
    assert len(spans) == 2
    assert spans[1][0] < spans[0][1], "the two intervals do not actually overlap"
    assert spans[1][0].month == 6 and spans[1][0].year == 2026


def test_gap_defect_really_leaves_days_uncovered_inside_june():
    rows = ownership_rows(MODEL, MBIDS[0], "temporal_gap")
    spans = sorted({(r["valid_from"], r["valid_to"]) for r in rows})
    assert spans[0][1] < spans[1][0], "there is no gap between the intervals"
    uncovered = dt.date(2026, 6, 10)
    assert not any(covers(f, t, uncovered) for f, t in spans)


def test_missing_holder_defect_points_at_a_holder_that_does_not_exist():
    rows = ownership_rows(MODEL, MBIDS[0], "missing_rights_holder")
    ids = {r["rights_holder_id"] for r in rows}
    assert any(int(i.removeprefix(MODEL.holder_id_prefix)) >= MODEL.holder_count for i in ids)


def test_healthy_recordings_are_not_quietly_defective():
    for mbid in MBIDS[:300]:
        rows = ownership_rows(MODEL, mbid)
        for valid_from in {r["valid_from"] for r in rows}:
            group = [r for r in rows if r["valid_from"] == valid_from]
            assert sum(r["share_pct"] for r in group) == Decimal("100.0000")
            assert group[0]["valid_to"] > valid_from
            assert len({r["split_version_id"] for r in group}) == 1


# --- rate card -----------------------------------------------------------------------------


def test_rate_card_uses_decimal_and_declares_currency_and_version():
    for row in rate_card_rows(MODEL):
        assert isinstance(row["rate_per_stream"], Decimal)
        assert row["currency"] == MODEL.rate_currency
        assert row["rule_version_id"] == MODEL.rule_version_id
        assert row["model_scope"] == MODEL.rate_model_scope


def test_rate_card_has_the_deliberate_three_day_gap():
    gaps = rate_gap_days(MODEL)
    assert gaps == [dt.date(2026, 6, 10), dt.date(2026, 6, 11), dt.date(2026, 6, 12)]
    assert len(gaps) == MODEL.expected_rate_gap_days


def test_rate_card_intervals_are_half_open_and_ordered():
    rows = sorted(rate_card_rows(MODEL), key=lambda r: r["valid_from"])
    for row in rows:
        assert row["valid_from"] < row["valid_to"]
    assert covers(rows[0]["valid_from"], rows[0]["valid_to"], dt.date(2026, 6, 9))
    assert not covers(rows[0]["valid_from"], rows[0]["valid_to"], dt.date(2026, 6, 10))
    assert covers(rows[1]["valid_from"], rows[1]["valid_to"], dt.date(2026, 6, 13))
