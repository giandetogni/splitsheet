"""Unit tests for the blocking builder's idempotency guard. No GCP, no credentials, no cost.

The risk covered is the one the audit found: the builder had no guard, so a rerun on
unchanged inputs would rebuild the candidate set and rewrite every published row for a
fresh timestamp alone -- tens of GiB of scan to reproduce what is already there.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from matching import build_blocking as mod

NORM = "1.0.0+0bc0dd643e06"
BLOCKING = "staged-1.0.0"
EXPECTED_RUN_ID = "blk:c005e9a56b1ec542"

PUBLISHED = {
    "n": 34466312, "runs": 1, "run_id": EXPECTED_RUN_ID,
    "nvers": 1, "nver": NORM, "bvers": 1, "bver": BLOCKING,
    "snaps": 1, "snap": mod.SNAPSHOT,
}


class ReachedHeavyWork(Exception):
    """Raised by the fake as soon as the builder asks for anything past the guard."""


def drive(monkeypatch, tmp_path, target_row, blocking=BLOCKING):
    """Run main() with the cloud replaced by a fake, and report which steps it reached."""
    calls: list[str] = []
    sqls: dict[str, str] = {}
    out = tmp_path / "blocking.json"

    def fake_run(client, sql, label, stats):
        calls.append(label)
        sqls[label] = sql
        if label == "inspect_target":
            stats.append({"step": label, "bytes_billed": 0, "slot_ms": 0})
            return [dict(target_row)] if target_row is not None else [
                {"n": 0, "runs": 0, "run_id": None, "nvers": 0, "nver": None,
                 "bvers": 0, "bver": None, "snaps": 0, "snap": None}]
        raise ReachedHeavyWork(label)

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "client_as_matcher", lambda project: object())
    monkeypatch.setattr(sys, "argv", [
        "build_blocking.py", "--project", "ss-de-944054e7",
        "--norm-version", NORM, "--blocking-version", blocking, "--out", str(out)])
    try:
        mod.main()
        return calls, json.loads(out.read_text()), sqls
    except ReachedHeavyWork as reached:
        return calls, reached, sqls


def test_an_existing_output_under_the_same_run_id_satisfies_the_guard(monkeypatch, tmp_path):
    _calls, report, _sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    assert isinstance(report, dict), "the guard should have returned before any heavy work"
    assert report["skipped"] is True


def test_the_guard_reaches_no_generation_and_no_write(monkeypatch, tmp_path):
    calls, _report, _sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    assert calls == ["inspect_target"], f"the builder went past the guard: {calls}"
    for step in ("stage_normalized", "stage_candidates", "publish_atomic", "drop_staging"):
        assert step not in calls


def test_the_guard_returns_the_identity_already_published(monkeypatch, tmp_path):
    _calls, report, _sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    assert report["candidate_run_id"] == EXPECTED_RUN_ID
    assert report["normalization_version"] == NORM
    assert report["blocking_version"] == BLOCKING
    assert report["candidate_rows"] == 34466312


def test_a_different_run_id_does_not_satisfy_the_guard(monkeypatch, tmp_path):
    calls, outcome, _sqls = drive(monkeypatch, tmp_path, {**PUBLISHED, "run_id": "blk:0000000000000000"})
    assert isinstance(outcome, ReachedHeavyWork)
    assert calls == ["inspect_target", "stage_normalized"]


def test_an_absent_output_does_not_satisfy_the_guard(monkeypatch, tmp_path):
    calls, outcome, _sqls = drive(monkeypatch, tmp_path, None)
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_normalized" in calls


@pytest.mark.parametrize("broken", [
    {"runs": 2}, {"nvers": 2}, {"bvers": 2}, {"snaps": 2},
    {"nver": "1.1.0+b3253b155934"}, {"bver": "staged-2.0.0"}, {"snap": "2020-01-01"},
    {"n": 0}, {"n": 1}, {"n": 34_466_311}, {"n": 34_466_313},
])
def test_any_mismatched_invariant_does_not_satisfy_the_guard(monkeypatch, tmp_path, broken):
    calls, outcome, _sqls = drive(monkeypatch, tmp_path, {**PUBLISHED, **broken})
    assert isinstance(outcome, ReachedHeavyWork), f"{broken} wrongly satisfied the guard"
    assert "stage_normalized" in calls


def test_the_normal_path_is_still_reachable(monkeypatch, tmp_path):
    """When the guard does not fire the builder proceeds exactly as before, starting with
    the normalized staging table."""
    _calls, outcome, _sqls = drive(monkeypatch, tmp_path, {**PUBLISHED, "run_id": "blk:stale"})
    assert isinstance(outcome, ReachedHeavyWork)
    assert str(outcome) == "stage_normalized"


def test_the_block_run_id_formula_is_unchanged():
    """The identity is derived from inputs alone; changing the formula would orphan
    blk:c005e9a56b1ec542."""
    body = inspect.getsource(mod.main)
    assert ('f"{args.norm_version}|{args.blocking_version}|{SNAPSHOT}|{EXPECTED_LISTENS}"'
            in body)
    assert '"blk:" + hashlib.sha256(' in body
    recomputed = "blk:" + hashlib.sha256(
        f"{NORM}|{BLOCKING}|{mod.SNAPSHOT}|{mod.EXPECTED_LISTENS}".encode()).hexdigest()[:16]
    assert recomputed == EXPECTED_RUN_ID


# The columns silver_match_candidates actually has. The guard first shipped asking for
# `snapshot_date`, which does not exist; the fakes could not catch it because they never
# send the SQL anywhere, so the column names are checked against this list instead.
CANDIDATE_COLUMNS = {
    "listen_hash", "candidate_recording_mbid", "block_method", "block_key",
    "canonical_snapshot_date", "blocking_version", "normalization_version",
    "candidate_run_id", "created_at",
}
GUARD_ALIASES = {"n", "runs", "run_id", "nvers", "nver", "bvers", "bver", "snaps", "snap"}


def test_the_guard_only_names_columns_the_table_has(monkeypatch, tmp_path):
    _calls, _report, sqls = drive(monkeypatch, tmp_path, PUBLISHED)
    sql = sqls["inspect_target"]
    assert "canonical_snapshot_date" in sql
    assert not re.search(r"\bsnapshot_date\b", sql), (
        "the guard names a snapshot_date column that silver_match_candidates does not have")
    # SQL keywords are written in caps, so the lowercase words are the identifiers.
    selected = set(re.findall(r"\b[a-z_]+\b", sql.split("FROM")[0]))
    unknown = selected - CANDIDATE_COLUMNS - GUARD_ALIASES
    assert unknown == set(), f"guard references unknown columns: {sorted(unknown)}"


def test_the_ratified_cardinality_is_pinned_to_the_proven_run():
    """34,466,312 is the physical row count measured for blk:c005e9a56b1ec542. It is not a
    standing expectation, and 3,045,208 -- the scored subset, which lives in another table
    under another phase -- is not one either."""
    assert mod.EXPECTED_CANDIDATES == {EXPECTED_RUN_ID: 34_466_312}


def test_an_unratified_run_id_does_not_inherit_the_proven_cardinality(monkeypatch, tmp_path):
    """A run built from different inputs derives a different id, and that id has no
    ratified count -- so a table matching on every other invariant, holding exactly the
    34,466,312 rows proven for the old run, still has to rebuild."""
    other = "staged-2.0.0"
    other_id = "blk:" + hashlib.sha256(
        f"{NORM}|{other}|{mod.SNAPSHOT}|{mod.EXPECTED_LISTENS}".encode()).hexdigest()[:16]
    assert other_id not in mod.EXPECTED_CANDIDATES
    calls, outcome, _sqls = drive(
        monkeypatch, tmp_path, {**PUBLISHED, "run_id": other_id, "bver": other},
        blocking=other)
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_normalized" in calls
