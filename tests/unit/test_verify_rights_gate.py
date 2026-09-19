"""Unit tests for the rights verification gate. No GCP, no credentials, no cost.

The risk covered is the one the audit found: verify_rights.py computed seven
injected-vs-detected reconciliations, wrote them into its report, printed OK or MISMATCH
-- and exited 0 either way. It is the last task before publish_period_results, so a green
step there reads as a clearance for the rights layer. A divergence has to fail.

The report is written before the gate runs, so the evidence of a failure survives it.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from rights import verify_rights as mod
from rights.generator import (
    load_rights_model,
    orphan_rows,
    ownership_rows,
    rate_card_rows,
    select_defects,
)

# The seven reconciliations, in the order verify_rights.py builds them, each paired with the
# key of the generator quota it is compared against.
SEVEN = [
    "shares_do_not_sum_to_100",
    "temporal_overlap",
    "temporal_gap",
    "invalid_interval",
    "missing_rights_holder",
    "orphan_recording_mbid",
    "holder_without_split",
]

GENERATION = json.loads(
    (pathlib.Path(__file__).parents[2] / "docs/phase0/rights_generation.json").read_text())
INJECTED = GENERATION["defects_injected"]


def agreeing_counts() -> dict[str, int]:
    """Detected counts that match every injected quota exactly."""
    return {
        "share_sum_sets": INJECTED["shares_do_not_sum_to_100"],
        "overlap_recordings": INJECTED["temporal_overlap"],
        "gap_recordings": INJECTED["temporal_gap"],
        "invalid_interval_sets": INJECTED["invalid_interval"],
        "missing_holder_sets": INJECTED["missing_rights_holder"],
        "orphan_recordings": INJECTED["orphan_recording_mbid"],
        "holders_without_split": INJECTED["holder_without_split_reserved"],
    }


# Which detected count feeds which reconciliation, so a test can break exactly one of them.
FEEDS = {
    "shares_do_not_sum_to_100": "share_sum_sets",
    "temporal_overlap": "overlap_recordings",
    "temporal_gap": "gap_recordings",
    "invalid_interval": "invalid_interval_sets",
    "missing_rights_holder": "missing_holder_sets",
    "orphan_recording_mbid": "orphan_recordings",
    "holder_without_split": "holders_without_split",
}


class FakeJob:
    def __init__(self, rows):
        self._rows = rows
        self.job_id = "fake-job"
        self.total_bytes_billed = 0
        self.slot_millis = 0

    def result(self):
        return self._rows


class FakeClient:
    """Answers each of the eight queries by what its SQL asks for. The production SQL is not
    read or rewritten here -- only matched on, so a query that changed shape would miss and
    raise rather than silently return the wrong canned rows."""

    def __init__(self, detected):
        self.detected = detected
        self.sqls: list[str] = []

    def query(self, sql, job_config=None):
        self.sqls.append(sql)
        d = self.detected
        if "matched_recording_mbid" in sql:
            return FakeJob([{"recording_mbid": f"mbid-{i}"} for i in range(2)])
        if "defect_share_sum_not_100" in sql:
            return FakeJob([{
                "share_sum_sets": d["share_sum_sets"],
                "overlap_recordings": d["overlap_recordings"],
                "gap_recordings": d["gap_recordings"],
                "invalid_interval_sets": d["invalid_interval_sets"],
                "missing_holder_sets": d["missing_holder_sets"],
                "orphan_recordings": d["orphan_recordings"],
                "total_sets": 2_522_403, "valid_sets": 2_519_953}])
        if "holders_without_split" in sql:
            return FakeJob([{"holders_without_split": d["holders_without_split"]}])
        if "recordings_probed" in sql:
            return FakeJob([{"recordings_probed": 200, "holders_changed": 200,
                             "holders_unchanged": 0}])
        if "resolution_status" in sql:
            return FakeJob([{"resolution_status": "RESOLVED", "recording_days": 1,
                             "streams": 1, "attributable": 1, "priced": 1}])
        if "payout_eligible" in sql:
            return FakeJob([{"payout_eligible": True, "hold_reason": None, "listens": 1}])
        if "dbt_valid_to" in sql:
            return FakeJob([{"versions": 60_250, "holders": 60_000, "current_versions": 60_000,
                             "closed_versions": 250, "holders_with_history": 250}])
        if "failure_rate" in sql:
            return FakeJob([{"rule": "r", "severity": "ERROR", "status": "PASS",
                             "failed_records": 0, "total_records": 1, "failure_rate": 0.0,
                             "business_impact": "b", "expectation": "e"}])
        raise AssertionError(f"unmatched query: {sql[:120]}")


def drive(monkeypatch, tmp_path, detected, row_counts_ok=True, second_pass_ok=True):
    """Run main() against the fake warehouse and report what happened."""
    out = tmp_path / "rights_verification.json"

    # The real frozen model, with only the holder count reduced: the base-holder digest would
    # otherwise regenerate 60,000 rows per test for a value the gate does not read.
    small = dataclasses.replace(load_rights_model(), holder_count=5)
    monkeypatch.setattr(mod, "load_rights_model", lambda *a, **k: small)

    # A landing artifact that matches what this reduced model regenerates, so row_counts_match
    # is true by construction and a test can make it false deliberately rather than by accident.
    mbids = [f"mbid-{i}" for i in range(2)]
    defects = select_defects(small, mbids)
    splits = sum(1 for m in mbids for _ in ownership_rows(small, m, defects.get(m)))
    splits += sum(1 for _ in orphan_rows(small))
    landed = {"rights_holders": small.holder_count,
              "ownership_splits": splits if row_counts_ok else splits + 1,
              "rate_card": len(rate_card_rows(small))}
    repo = tmp_path / "repo"
    (repo / "docs/phase0").mkdir(parents=True)
    (repo / "docs/phase0/rights_generation.json").write_text(json.dumps({
        "generation_run_id": GENERATION["generation_run_id"],
        "files": {k: {"rows": v} for k, v in landed.items()},
        "defects_injected": INJECTED}))
    monkeypatch.setattr(mod, "REPO", repo)

    if not second_pass_ok:
        # Make the generator disagree with itself on the rate card only, which is exactly what
        # second_pass_identical exists to catch, without disturbing any row count.
        real_digest = mod.digest_of
        seen = {"rate": 0}

        def drifting(columns, rows):
            digest = real_digest(columns, rows)
            if columns == mod.RATE_COLUMNS:
                seen["rate"] += 1
                if seen["rate"] > 1:
                    return digest[:-4] + "dead"
            return digest

        monkeypatch.setattr(mod, "digest_of", drifting)

    client = FakeClient(detected)
    fake_bq = types.SimpleNamespace(Client=lambda project: client,
                                    QueryJobConfig=lambda **k: None)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bq)
    import google.cloud
    monkeypatch.setattr(google.cloud, "bigquery", fake_bq, raising=False)

    monkeypatch.setattr(sys, "argv", ["verify_rights.py", "--out", str(out)])
    try:
        mod.main()
        failure = None
    except SystemExit as e:
        failure = str(e)
    report = json.loads(out.read_text()) if out.exists() else None
    return failure, report, client


def test_all_nine_conditions_holding_exits_cleanly(monkeypatch, tmp_path):
    failure, report, _client = drive(monkeypatch, tmp_path, agreeing_counts())
    assert failure is None, f"a fully reconciled run must not fail: {failure}"
    assert report["reconciliation_injected_vs_detected"]["all_agree"] is True


def test_the_successful_run_still_writes_the_whole_report(monkeypatch, tmp_path):
    """The success path is unchanged: same sections, same seven reconciliations."""
    _failure, report, _client = drive(monkeypatch, tmp_path, agreeing_counts())
    for section in ("declaration", "versions", "reproducibility",
                    "reconciliation_injected_vs_detected", "temporal_proof",
                    "resolution_status", "payout_eligibility", "scd2", "quality_report",
                    "cost"):
        assert section in report
    rec = report["reconciliation_injected_vs_detected"]
    assert sorted(k for k in rec if k != "all_agree") == sorted(SEVEN)


@pytest.mark.parametrize("broken", SEVEN)
def test_any_one_of_the_seven_diverging_fails_the_task(monkeypatch, tmp_path, broken):
    """Every one of the seven is in the decision, not just the first or the last."""
    counts = agreeing_counts()
    counts[FEEDS[broken]] += 1
    failure, report, _client = drive(monkeypatch, tmp_path, counts)
    assert failure is not None, f"{broken} diverged and the task still succeeded"
    assert broken in failure
    assert report["reconciliation_injected_vs_detected"][broken]["agrees"] is False


def test_several_diverging_are_all_named(monkeypatch, tmp_path):
    counts = agreeing_counts()
    for k in ("shares_do_not_sum_to_100", "temporal_gap", "holder_without_split"):
        counts[FEEDS[k]] += 7
    failure, _report, _client = drive(monkeypatch, tmp_path, counts)
    assert failure is not None
    for k in ("shares_do_not_sum_to_100", "temporal_gap", "holder_without_split"):
        assert k in failure
    assert "temporal_overlap" not in failure


def test_the_report_survives_the_failure(monkeypatch, tmp_path):
    """The report is written before the gate, so the evidence outlives the exit."""
    counts = agreeing_counts()
    counts["orphan_recordings"] -= 1
    failure, report, _client = drive(monkeypatch, tmp_path, counts)
    assert failure is not None
    assert report is not None, "the report must exist even when the task fails"
    rec = report["reconciliation_injected_vs_detected"]
    assert rec["all_agree"] is False
    assert rec["orphan_recording_mbid"]["detected"] == INJECTED["orphan_recording_mbid"] - 1


def test_the_failure_message_carries_injected_and_detected(monkeypatch, tmp_path):
    counts = agreeing_counts()
    counts["invalid_interval_sets"] = 0
    failure, _report, _client = drive(monkeypatch, tmp_path, counts)
    assert "invalid_interval" in failure
    assert f"injected={INJECTED['invalid_interval']}" in failure
    assert "detected=0" in failure


def test_the_expected_quotas_come_from_the_frozen_generation_artifact(monkeypatch, tmp_path):
    """No threshold is introduced or relaxed: the expectations are still the numbers the
    generator recorded when it landed the data."""
    _failure, report, _client = drive(monkeypatch, tmp_path, agreeing_counts())
    rec = report["reconciliation_injected_vs_detected"]
    for name in SEVEN:
        quota = "holder_without_split_reserved" if name == "holder_without_split" else name
        assert rec[name]["injected"] == INJECTED[quota]


def test_the_reconciliation_sql_is_unchanged(monkeypatch, tmp_path):
    """The gate reads the seven counts; it does not rewrite the queries that produce them."""
    _failure, _report, client = drive(monkeypatch, tmp_path, agreeing_counts())
    defects_sql = next(s for s in client.sqls if "defect_share_sum_not_100" in s)
    for column in ("defect_temporal_overlap", "defect_temporal_gap", "defect_invalid_interval",
                   "defect_missing_rights_holder", "defect_orphan_recording", "is_valid_set"):
        assert column in defects_sql
    assert "splitsheet_dbt.int_ownership_validity" in defects_sql
    holders_sql = next(s for s in client.sqls if "holders_without_split" in s)
    assert "splitsheet_rights.rights_holders" in holders_sql
    assert "splitsheet_rights.ownership_splits" in holders_sql
    assert len(client.sqls) == 8, f"the task should still run eight queries: {len(client.sqls)}"


def test_row_counts_not_matching_fails_even_when_all_seven_reconcile(monkeypatch, tmp_path):
    """Rights that no longer regenerate to the landed size cannot support a published payout,
    whatever the defect counts say."""
    failure, report, _client = drive(monkeypatch, tmp_path, agreeing_counts(),
                                     row_counts_ok=False)
    assert failure is not None
    assert "row_counts_match" in failure
    assert report["reproducibility"]["row_counts_match"] is False
    assert report["reconciliation_injected_vs_detected"]["all_agree"] is True


def test_a_non_deterministic_second_pass_fails_even_when_all_seven_reconcile(
        monkeypatch, tmp_path):
    failure, report, _client = drive(monkeypatch, tmp_path, agreeing_counts(),
                                     second_pass_ok=False)
    assert failure is not None
    assert "second_pass_identical" in failure
    assert report["reproducibility"]["second_pass_identical"] is False
    assert report["reconciliation_injected_vs_detected"]["all_agree"] is True


def test_both_reproducibility_failures_are_named_together(monkeypatch, tmp_path):
    failure, report, _client = drive(monkeypatch, tmp_path, agreeing_counts(),
                                     row_counts_ok=False, second_pass_ok=False)
    assert failure is not None
    assert "row_counts_match" in failure and "second_pass_identical" in failure
    assert report is not None


def test_the_report_survives_a_reproducibility_failure(monkeypatch, tmp_path):
    """Same contract as for the seven: the evidence is on disk before the gate runs."""
    _failure, report, _client = drive(monkeypatch, tmp_path, agreeing_counts(),
                                      row_counts_ok=False)
    assert report is not None
    assert report["reproducibility"]["row_counts_regenerated"] != \
        report["reproducibility"]["row_counts_landed"]


def test_a_reconciliation_and_a_reproducibility_failure_are_both_named(monkeypatch, tmp_path):
    counts = agreeing_counts()
    counts["gap_recordings"] += 3
    failure, _report, _client = drive(monkeypatch, tmp_path, counts, second_pass_ok=False)
    assert "temporal_gap" in failure
    assert "second_pass_identical" in failure
