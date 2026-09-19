"""Unit tests for the publication/promotion split. No GCP, no credentials, no cost.

The risk covered is the one the audit found: publish.py called set_pointer unconditionally,
so publishing was the same act as promoting. The attribution_run_id the frozen policy derives
today is attr:83c013596d1d3da3 -- pub:v1 -- while CURRENT stands at attr:fb74b680430fa0b2,
pub:v2. A scheduled publication would therefore have rolled the publication in force back to
before the Phase 6 restatement, silently, with no flag.

Publishing and validating must never move the pointer. --move-pointer is the one flag that
authorises touching CURRENT.
"""

from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from payout import publish as mod
from payout.policy import attribution_run_id, load_payout_policy

POLICY = load_payout_policy()
PUBLISHED_RUN_ID = attribution_run_id(POLICY, "PUBLISHED")


class FakeClient:
    pass


def fake_rows(label: str, run_id: str):
    """One canned answer per labelled query, keyed the way run_query is called."""
    if label == "verify frozen inputs":
        return [{"match_runs": 1, "match_run_id": POLICY.match_run_id,
                 "scoring_version": POLICY.scoring_version,
                 "rights_version": POLICY.rights_version,
                 "rights_generation_run_id": POLICY.rights_generation_run_id,
                 "rule_version_id": POLICY.rule_version_id}]
    if label == "ensure pointer table":
        return []
    if label.endswith(": published_at"):
        return [{"first_published_at": "2026-08-15 23:47:45.331701+00:00"}]
    if "digest" in label:
        return [{"row_count": 5_925_913, "content_digest": "51193c1f2c4e8fcd",
                 "total_holder_payout": "98284.22"}]
    if label == "current view while pointer moved away":
        return [{"n": 5_925_913}]
    if label == "reconciliation":
        return [{"attribution_status": "ATTRIBUTABLE", "listens": 28_386_887, "streams": 1,
                 "pct_of_all_listens": 74.3, "distinct_recordings": 1,
                 "total_gross_royalty": "98284.22", "total_holder_payout": "98284.22",
                 "holder_rows": 5_925_913, "recordings_paid": 1, "holders_paid": 1}]
    if label == "money summary":
        return [{"holder_rows": 5_925_913, "total_holder_payout": "98284.22",
                 "min_holder_payout": "0.01", "max_holder_payout": "1.00",
                 "negative_payouts": 0, "zero_payouts": 0, "holders_paid": 1,
                 "recordings_paid": 1, "financial_groups": 1,
                 "total_gross_royalty": "98284.22", "remainder_cents_distributed": 0,
                 "groups_that_do_not_close": 0}]
    return [{}]


def drive(monkeypatch, tmp_path, argv_extra, digest_rows=5_925_913, fail_before_publish=False):
    """Run main() against fakes and report how often the pointer was set, and to what."""
    out = tmp_path / "payout_publication.json"
    pointer_calls: list[tuple[str, str]] = []
    queries: list[str] = []
    dbt_calls: list[str] = []

    def fake_run_query(client, sql, label, stats, dry_run=False):
        queries.append(label)
        if dry_run:
            stats.append({"step": label, "dry_run": True, "estimated_bytes": 0})
            return None
        stats.append({"step": label, "job_id": "fake", "bytes_billed": 0, "slot_ms": 0,
                      "duration_ms": 0})
        rows = fake_rows(label, PUBLISHED_RUN_ID)
        if "digest" in label and not label.endswith(": published_at"):
            rows = [{**rows[0], "row_count": digest_rows}]
        return rows

    def fake_set_pointer(client, run_id, policy_version, note, stats):
        pointer_calls.append((run_id, note))

    def fake_dbt(args, dbt_vars, label):
        dbt_calls.append(label)
        if fail_before_publish and label == "build financial layer":
            raise SystemExit("dbt build financial layer failed with exit 1")
        return {"label": label, "exit_code": 0, "seconds": 0.0, "summary": "Done."}

    monkeypatch.setattr(mod, "run_query", fake_run_query)
    monkeypatch.setattr(mod, "set_pointer", fake_set_pointer)
    monkeypatch.setattr(mod, "dbt", fake_dbt)
    monkeypatch.setattr(mod, "bq_client", lambda: FakeClient())
    monkeypatch.setattr(mod, "publication_digest_sql", lambda table, run_id: "SELECT 1")
    monkeypatch.setattr(sys, "argv",
                        ["publish.py", "--out", str(out), "--skip-dry-run", *argv_extra])
    failure = None
    try:
        mod.main()
    except SystemExit as e:
        failure = e
    report = json.loads(out.read_text()) if out.exists() else None
    return types.SimpleNamespace(pointer_calls=pointer_calls, queries=queries,
                                 dbt_calls=dbt_calls, failure=failure, report=report)


# --- the pointer is not moved unless it is explicitly authorised ------------------------

def test_the_default_run_does_not_move_the_pointer(monkeypatch, tmp_path):
    """This is the scheduler's command: publish and validate, promote nothing."""
    r = drive(monkeypatch, tmp_path, [])
    assert r.pointer_calls == [], "a plain publication must not touch CURRENT"
    assert r.failure is None


def test_publishing_without_the_flag_still_completes(monkeypatch, tmp_path):
    """The publication path is unchanged: the build runs, the digest is taken, the report is
    written. Only the promotion is withheld."""
    r = drive(monkeypatch, tmp_path, [])
    assert "build financial layer" in r.dbt_calls
    assert any("digest" in q for q in r.queries)
    assert r.report is not None
    assert r.pointer_calls == []


def test_prove_pointer_move_alone_is_refused_and_moves_nothing(monkeypatch, tmp_path):
    """It moves the pointer away and back, so it cannot be its own authorisation."""
    r = drive(monkeypatch, tmp_path, ["--prove-pointer-move"])
    assert r.pointer_calls == []
    assert isinstance(r.failure, SystemExit)
    assert r.failure.code == 2, "argparse should refuse before anything runs"
    assert r.queries == [], "nothing should reach the warehouse"


def test_the_explicit_flag_moves_the_pointer_exactly_once(monkeypatch, tmp_path):
    r = drive(monkeypatch, tmp_path, ["--move-pointer"])
    assert len(r.pointer_calls) == 1, f"expected one promotion, got {r.pointer_calls}"
    assert r.pointer_calls[0][0] == PUBLISHED_RUN_ID
    assert r.failure is None


def test_both_flags_together_are_accepted(monkeypatch, tmp_path):
    """--prove-pointer-move is a demonstration on top of a promotion, not a substitute."""
    r = drive(monkeypatch, tmp_path, ["--move-pointer", "--prove-pointer-move"])
    assert r.failure is None
    assert r.pointer_calls[0][0] == PUBLISHED_RUN_ID
    assert len(r.pointer_calls) > 1, "the proof moves the pointer away and restores it"


# --- the pointer is not moved when the publication is not there -------------------------

def test_move_pointer_without_a_valid_publication_fails_and_moves_nothing(
        monkeypatch, tmp_path):
    """The precondition for promoting is a publication that exists. Zero rows is refused
    before the pointer is touched."""
    r = drive(monkeypatch, tmp_path, ["--move-pointer"], digest_rows=0)
    assert r.pointer_calls == []
    assert isinstance(r.failure, SystemExit)
    assert "produced no rows" in str(r.failure)


def test_a_failure_before_publication_leaves_the_pointer_alone(monkeypatch, tmp_path):
    r = drive(monkeypatch, tmp_path, ["--move-pointer"], fail_before_publish=True)
    assert r.pointer_calls == []
    assert isinstance(r.failure, SystemExit)


def test_an_existing_publication_is_not_promoted_without_the_flag(monkeypatch, tmp_path):
    """The no-op case: the rows are already there, the digest matches, nothing is inserted --
    and CURRENT is still not moved."""
    r = drive(monkeypatch, tmp_path, [])
    assert r.report["proofs"]["first_publication"]["rows_published"] == "5925913"
    assert r.pointer_calls == []


# --- nothing else moved -----------------------------------------------------------------

def test_the_attribution_identity_is_unchanged(monkeypatch, tmp_path):
    """The run id is still derived from the frozen policy inputs and the label, with no
    wall-clock. Changing it would orphan every published row."""
    assert PUBLISHED_RUN_ID == "attr:83c013596d1d3da3"
    assert attribution_run_id(POLICY, "REHEARSAL") != PUBLISHED_RUN_ID
    r = drive(monkeypatch, tmp_path, [])
    assert r.report["attribution_run_id"] == PUBLISHED_RUN_ID


def test_the_publication_checks_are_still_run(monkeypatch, tmp_path):
    """Frozen-input verification, the digest and the money closure all still happen on the
    default path: the patch withholds the promotion, not the verification."""
    r = drive(monkeypatch, tmp_path, [])
    assert "verify frozen inputs" in r.queries
    assert "money summary" in r.queries
    assert "reconciliation" in r.queries
    assert any("digest" in q for q in r.queries)


def test_the_report_records_whether_the_pointer_moved(monkeypatch, tmp_path):
    without = drive(monkeypatch, tmp_path, [])
    assert without.report["proofs"]["pointer_moved"] is False


def test_the_report_records_a_promotion_when_it_happens(monkeypatch, tmp_path):
    with_flag = drive(monkeypatch, tmp_path, ["--move-pointer"])
    assert with_flag.report["proofs"]["pointer_moved"] is True


@pytest.mark.parametrize("flag", ["--move-pointer", "--prove-pointer-move"])
def test_the_scheduler_command_carries_no_pointer_authorisation(flag):
    """The DAG's rendered command must never contain either flag, so a scheduled run cannot
    promote. Read from the task graph rather than from a copy of the string."""
    sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "dags"))
    from pipeline_spec import TASKS

    publish = next(t for t in TASKS if t.task_id == "publish_period_results")
    assert flag not in publish.command
    assert "--force" not in publish.command
