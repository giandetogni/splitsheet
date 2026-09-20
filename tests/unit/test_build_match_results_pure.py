"""Unit tests for the match builder's idempotency guard. No GCP, no credentials, no cost.

The risk covered is the one the audit found: the builder regenerated unconditionally, so a
rerun on unchanged inputs would rescore the candidates and rewrite all 38,199,641 published
rows with a fresh matched_at -- about 30.9 GB to reproduce what is already there.

The second risk is narrower and cost a runtime failure once already, on the blocking guard:
silver_listen_matches requires a partition filter, so a guard that reads it without one is
rejected by BigQuery before it can read anything. The fakes here cannot catch that on their
own, so the PERIOD filter is asserted against the emitted SQL.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from matching import build_match_results as mod
from matching import features as F
from matching.scoring import load_scoring_rules

NORM = "1.0.0+0bc0dd643e06"
BLOCKING = "staged-1.0.0"
CANDIDATE = "blk:c005e9a56b1ec542"
SCORING = "1.0.0+cb21f9704ff0"
EXPECTED_RUN_ID = "match:101eef5c5b5c081e"

PUBLISHED = {
    "n": 38_199_641,
    "mruns": 1,
    "mrun": EXPECTED_RUN_ID,
    "cruns": 1,
    "crun": CANDIDATE,
    "svers": 1,
    "sver": SCORING,
    "nvers": 1,
    "nver": NORM,
    "bvers": 1,
    "bver": BLOCKING,
}


class ReachedHeavyWork(Exception):
    """Raised by the fake as soon as the builder asks for anything past the guard."""


# stage_sql(rules, ...) is built as an argument to run(), so it is evaluated before the
# fake can refuse it: the rules object has to carry every attribute it reads. Borrowing the
# real frozen rules and relabelling only the version keeps that faithful without stubbing
# the scoring internals.
REAL_RULES = load_scoring_rules()


class RulesAtVersion:
    def __init__(self, version: str) -> None:
        self._version = version

    def __getattr__(self, name):
        return getattr(REAL_RULES, name)

    @property
    def version(self) -> str:
        return self._version


def drive(
    monkeypatch,
    tmp_path,
    target_row,
    norm=NORM,
    blocking=BLOCKING,
    candidate=CANDIDATE,
    scoring=SCORING,
):
    """Run main() with the cloud replaced by a fake, and report which steps it reached."""
    calls: list[str] = []
    sqls: dict[str, str] = {}
    out = tmp_path / "match_results.json"

    rules = RulesAtVersion(scoring)

    def fake_run(client, sql, label, stats):
        calls.append(label)
        sqls[label] = sql
        if label == "inspect_target":
            stats.append({"step": label, "bytes_billed": 0, "slot_ms": 0})
            return (
                [dict(target_row)]
                if target_row is not None
                else [
                    {
                        "n": 0,
                        "mruns": 0,
                        "mrun": None,
                        "cruns": 0,
                        "crun": None,
                        "svers": 0,
                        "sver": None,
                        "nvers": 0,
                        "nver": None,
                        "bvers": 0,
                        "bver": None,
                    }
                ]
            )
        raise ReachedHeavyWork(label)

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "client_as_matcher", lambda: object())
    monkeypatch.setattr(mod, "load_scoring_rules", lambda *a, **k: rules)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_match_results.py",
            "--norm-version",
            norm,
            "--blocking-version",
            blocking,
            "--candidate-run-id",
            candidate,
            "--out",
            str(out),
        ],
    )
    try:
        mod.main()
        return calls, json.loads(out.read_text()), sqls
    except ReachedHeavyWork as reached:
        return calls, reached, sqls


def test_an_existing_period_under_the_same_identity_satisfies_the_guard(monkeypatch, tmp_path):
    _calls, report, _sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    assert isinstance(report, dict), "the guard should have returned before any heavy work"
    assert report["skipped"] is True


def test_the_guard_creates_no_staging_and_scores_nothing(monkeypatch, tmp_path):
    calls, _report, _sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    assert calls == ["inspect_target"], f"the builder went past the guard: {calls}"
    for step in (
        "stage_matches",
        "validate_matches",
        "publish_atomic",
        "drop_staging",
        "distribution",
        "coverage",
    ):
        assert step not in calls


def test_the_guard_returns_the_identity_already_published(monkeypatch, tmp_path):
    _calls, report, _sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    assert report["match_run_id"] == EXPECTED_RUN_ID
    assert report["candidate_run_id"] == CANDIDATE
    assert report["scoring_version"] == SCORING
    assert report["normalization_version"] == NORM
    assert report["blocking_version"] == BLOCKING
    assert report["matched_rows"] == 38_199_641


@pytest.mark.parametrize(
    "broken",
    [
        {"n": 38_199_640},
        {"n": 38_199_642},
        {"n": 0},
        {"n": 1},
        {"mrun": "match:0000000000000000"},
        {"mruns": 2},
        {"cruns": 2},
        {"svers": 2},
        {"nvers": 2},
        {"bvers": 2},
    ],
)
def test_any_mismatched_invariant_does_not_satisfy_the_guard(monkeypatch, tmp_path, broken):
    calls, outcome, _sqls = drive(monkeypatch, tmp_path, {**PUBLISHED, **broken})
    assert isinstance(outcome, ReachedHeavyWork), f"{broken} wrongly satisfied the guard"
    assert "stage_matches" in calls


@pytest.mark.parametrize(
    "field,kwarg,other",
    [
        ("crun", "candidate", "blk:0000000000000000"),
        ("sver", "scoring", "2.0.0+ffffffffffff"),
        ("nver", "norm", "1.1.0+b3253b155934"),
        ("bver", "blocking", "staged-2.0.0"),
    ],
)
def test_an_output_built_from_another_input_does_not_satisfy_the_guard(
    monkeypatch, tmp_path, field, kwarg, other
):
    """Each of these is in the run id, so a table written under a different one carries a
    different identity -- and the column has to disagree too."""
    calls, outcome, _sqls = drive(
        monkeypatch, tmp_path, {**PUBLISHED, field: other}, **{kwarg: other}
    )
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_matches" in calls


def test_an_absent_output_does_not_satisfy_the_guard(monkeypatch, tmp_path):
    calls, outcome, _sqls = drive(monkeypatch, tmp_path, None)
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_matches" in calls


def test_the_normal_path_is_still_reachable(monkeypatch, tmp_path):
    """When the guard does not fire the builder proceeds exactly as before, starting with
    the match staging table."""
    _calls, outcome, _sqls = drive(monkeypatch, tmp_path, {**PUBLISHED, "n": 7})
    assert isinstance(outcome, ReachedHeavyWork)
    assert str(outcome) == "stage_matches"


def test_the_guard_query_carries_the_required_partition_filter(monkeypatch, tmp_path):
    """silver_listen_matches has require_partition_filter: without a listened_at predicate
    BigQuery rejects the query outright, and the fakes above would never notice."""
    _calls, _report, sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    sql = sqls["inspect_target"]
    assert "silver_listen_matches" in sql
    assert "WHERE" in sql and mod.PERIOD in sql
    assert "listened_at" in sql


def test_the_match_run_id_formula_is_unchanged():
    """The identity is derived from inputs alone; changing the formula would orphan
    match:101eef5c5b5c081e."""
    # Whitespace-collapsed, so the assertions below pin the formula rather than the
    # line wrapping a formatter chose for it.
    body = " ".join(inspect.getsource(mod.main).split())
    assert '"match:" + hashlib.sha256(' in body
    assert "{args.norm_version}|{args.blocking_version}|{args.candidate_run_id}|" in body
    assert "{rules.version}|{F.FEATURE_VERSION}" in body
    recomputed = (
        "match:"
        + hashlib.sha256(
            f"{NORM}|{BLOCKING}|{CANDIDATE}|{SCORING}|{F.FEATURE_VERSION}".encode()
        ).hexdigest()[:16]
    )
    assert recomputed == EXPECTED_RUN_ID


def test_the_ratified_cardinality_is_one_row_per_listen():
    """38,199,641 is this table's grain -- one row per listen. The 3,045,208 scored pairs
    and the 34,466,312 candidate pairs are inputs, at other grains."""
    assert mod.EXPECTED_LISTENS == 38_199_641
