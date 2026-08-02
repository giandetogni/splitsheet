"""Unit tests for the frozen dev/holdout split. Pure: no GCP."""

from __future__ import annotations

import pytest

from evaluation import DEV, HOLDOUT, bucket_of, load_split_config

CFG = load_split_config()

#: Pinned. The split must never change silently, because every comparison made under the
#: old split would become meaningless.
EXPECTED_SPLIT_VERSION = "1.0.0"

MBIDS = [f"00000000-0000-4000-8000-{i:012d}" for i in range(4000)]


def test_split_version_is_pinned():
    assert CFG.split_version == EXPECTED_SPLIT_VERSION


def test_assignment_is_deterministic():
    first = [bucket_of(m, CFG) for m in MBIDS]
    second = [bucket_of(m, CFG) for m in MBIDS]
    assert first == second


def test_assignment_does_not_depend_on_order():
    forward = {m: bucket_of(m, CFG) for m in MBIDS}
    backward = {m: bucket_of(m, CFG) for m in reversed(MBIDS)}
    assert forward == backward


def test_split_is_close_to_eighty_twenty():
    buckets = [bucket_of(m, CFG) for m in MBIDS]
    dev_share = 100 * buckets.count(DEV) / len(buckets)
    assert 76 < dev_share < 84, f"dev share {dev_share:.1f}% is far from the intended 80%"
    assert buckets.count(DEV) + buckets.count(HOLDOUT) == len(MBIDS)


def test_absent_label_is_neither_dev_nor_holdout():
    for missing in (None, ""):
        assert bucket_of(missing, CFG) == CFG.unlabelled_bucket


def test_same_recording_always_lands_on_the_same_side():
    """Splitting by recording, not by listen, is what keeps the sides disjoint."""
    mbid = MBIDS[7]
    assert len({bucket_of(mbid, CFG) for _ in range(50)}) == 1


def test_sql_expression_is_generated_from_the_same_config():
    sql = CFG.sql_expression()
    assert CFG.salt in sql
    assert str(CFG.dev_upper_exclusive) in sql
    assert str(CFG.modulus) in sql
    assert CFG.unlabelled_bucket in sql


@pytest.mark.parametrize("bad", [
    {"algorithm": "coinflip"},
    {"buckets": {"dev": {"lower_inclusive": 1, "upper_exclusive": 80},
                 "holdout": {"lower_inclusive": 80, "upper_exclusive": 100}}},
    {"buckets": {"dev": {"lower_inclusive": 0, "upper_exclusive": 70},
                 "holdout": {"lower_inclusive": 80, "upper_exclusive": 100}}},
])
def test_malformed_split_config_is_rejected(tmp_path, bad):
    import yaml

    from evaluation import load_split_config as load

    with open("config/evaluation_split.yml") as fh:
        raw = yaml.safe_load(fh)
    raw.update(bad)
    p = tmp_path / "bad.yml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load(p)
