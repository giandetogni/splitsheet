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


def drive(monkeypatch, tmp_path, target_row):
    """Run main() with the cloud replaced by a fake, and report which steps it reached."""
    calls: list[str] = []
    out = tmp_path / "blocking.json"

    def fake_run(client, sql, label, stats):
        calls.append(label)
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
        "--norm-version", NORM, "--blocking-version", BLOCKING, "--out", str(out)])
    try:
        mod.main()
        return calls, json.loads(out.read_text())
    except ReachedHeavyWork as reached:
        return calls, reached


def test_an_existing_output_under_the_same_run_id_satisfies_the_guard(monkeypatch, tmp_path):
    _calls, report = drive(monkeypatch, tmp_path, PUBLISHED)
    assert isinstance(report, dict), "the guard should have returned before any heavy work"
    assert report["skipped"] is True


def test_the_guard_reaches_no_generation_and_no_write(monkeypatch, tmp_path):
    calls, _ = drive(monkeypatch, tmp_path, PUBLISHED)
    assert calls == ["inspect_target"], f"the builder went past the guard: {calls}"
    for step in ("stage_normalized", "stage_candidates", "publish_atomic", "drop_staging"):
        assert step not in calls


def test_the_guard_returns_the_identity_already_published(monkeypatch, tmp_path):
    _, report = drive(monkeypatch, tmp_path, PUBLISHED)
    assert report["candidate_run_id"] == EXPECTED_RUN_ID
    assert report["normalization_version"] == NORM
    assert report["blocking_version"] == BLOCKING
    assert report["candidate_rows"] == 34466312


def test_a_different_run_id_does_not_satisfy_the_guard(monkeypatch, tmp_path):
    calls, outcome = drive(monkeypatch, tmp_path, {**PUBLISHED, "run_id": "blk:0000000000000000"})
    assert isinstance(outcome, ReachedHeavyWork)
    assert calls == ["inspect_target", "stage_normalized"]


def test_an_absent_output_does_not_satisfy_the_guard(monkeypatch, tmp_path):
    calls, outcome = drive(monkeypatch, tmp_path, None)
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_normalized" in calls


@pytest.mark.parametrize("broken", [
    {"runs": 2}, {"nvers": 2}, {"bvers": 2}, {"snaps": 2},
    {"nver": "1.1.0+b3253b155934"}, {"bver": "staged-2.0.0"}, {"snap": "2020-01-01"},
    {"n": 0},
])
def test_any_mismatched_invariant_does_not_satisfy_the_guard(monkeypatch, tmp_path, broken):
    calls, outcome = drive(monkeypatch, tmp_path, {**PUBLISHED, **broken})
    assert isinstance(outcome, ReachedHeavyWork), f"{broken} wrongly satisfied the guard"
    assert "stage_normalized" in calls


def test_the_normal_path_is_still_reachable(monkeypatch, tmp_path):
    """When the guard does not fire the builder proceeds exactly as before, starting with
    the normalized staging table."""
    _calls, outcome = drive(monkeypatch, tmp_path, {**PUBLISHED, "run_id": "blk:stale"})
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
