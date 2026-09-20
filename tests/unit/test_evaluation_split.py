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


@pytest.mark.parametrize(
    "bad",
    [
        {"algorithm": "coinflip"},
        {
            "buckets": {
                "dev": {"lower_inclusive": 1, "upper_exclusive": 80},
                "holdout": {"lower_inclusive": 80, "upper_exclusive": 100},
            }
        },
        {
            "buckets": {
                "dev": {"lower_inclusive": 0, "upper_exclusive": 70},
                "holdout": {"lower_inclusive": 80, "upper_exclusive": 100},
            }
        },
    ],
)
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


# --- the second division: calibration and validation inside the old dev -----------------
#
# Added in Phase 4B after the original holdout was acknowledged as consumed. The dev/holdout
# rule above is unchanged; these tests cover the division inside dev.

EXPECTED_CALIBRATION_SPLIT_VERSION = "1.0.0"


def test_calibration_split_version_is_pinned():
    assert CFG.calibration_split_version == EXPECTED_CALIBRATION_SPLIT_VERSION


def test_partition_is_deterministic_and_order_independent():
    from evaluation import partition_of

    first = {m: partition_of(m, CFG) for m in MBIDS}
    second = {m: partition_of(m, CFG) for m in reversed(MBIDS)}
    assert first == second


def test_partitions_are_exactly_calibration_validation_holdout():
    from evaluation import CALIBRATION, HOLDOUT, VALIDATION, partition_of

    seen = {partition_of(m, CFG) for m in MBIDS}
    assert seen == {CALIBRATION, VALIDATION, HOLDOUT}


def test_no_recording_is_in_both_calibration_and_validation():
    """Disjointness by construction: the partition is a function of the recording."""
    from evaluation import CALIBRATION, VALIDATION, partition_of

    cal = {m for m in MBIDS if partition_of(m, CFG) == CALIBRATION}
    val = {m for m in MBIDS if partition_of(m, CFG) == VALIDATION}
    assert not (cal & val)
    assert cal and val


def test_the_division_only_touches_dev():
    """holdout must survive the second division untouched, or the historical numbers move."""
    from evaluation import CALIBRATION, DEV, HOLDOUT, VALIDATION, bucket_of, partition_of

    for m in MBIDS:
        if bucket_of(m, CFG) == HOLDOUT:
            assert partition_of(m, CFG) == HOLDOUT
        else:
            assert bucket_of(m, CFG) == DEV
            assert partition_of(m, CFG) in (CALIBRATION, VALIDATION)


def test_calibration_is_about_three_quarters_of_dev():
    from evaluation import CALIBRATION, DEV, bucket_of, partition_of

    dev = [m for m in MBIDS if bucket_of(m, CFG) == DEV]
    share = 100 * sum(partition_of(m, CFG) == CALIBRATION for m in dev) / len(dev)
    assert 70 < share < 80, f"calibration share of dev is {share:.1f}%, intended 75%"


def test_calibration_salt_is_different_from_the_dev_salt():
    """Same salt would make the second division a recut of the first, not an independent one."""
    assert CFG.calibration_salt != CFG.salt


def test_absent_label_is_in_no_partition():
    from evaluation import partition_of

    for missing in (None, ""):
        assert partition_of(missing, CFG) == CFG.unlabelled_bucket


def test_partition_sql_is_generated_from_the_same_config_values():
    sql = CFG.partition_sql_expression("mapper_recording_mbid")
    assert CFG.calibration_salt in sql
    assert str(CFG.calibration_upper_exclusive) in sql
    assert CFG.salt in sql
    assert "calibration" in sql and "validation" in sql and "holdout" in sql
