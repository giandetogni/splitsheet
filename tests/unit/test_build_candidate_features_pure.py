"""Unit tests for the feature builder's idempotency guard. No GCP, no credentials, no cost.

The risk covered is the one the audit found: the builder regenerated unconditionally, so a
rerun on unchanged inputs would restage the scored universe, rejoin it against the
canonical texts and rewrite all 3,045,208 published rows with a fresh CURRENT_TIMESTAMP --
about 12.4 GB to reproduce what is already there.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from matching import build_candidate_features as mod
from matching import features as F

NORM = "1.0.0+0bc0dd643e06"
CANDIDATE = "blk:c005e9a56b1ec542"
EXPECTED_RUN_ID = "feat:7411e987640a1051"

PUBLISHED = {
    "n": 3_045_208,
    "fruns": 1, "frun": EXPECTED_RUN_ID,
    "cruns": 1, "crun": CANDIDATE,
    "nvers": 1, "nver": NORM,
    "fvers": 1, "fver": F.FEATURE_VERSION,
}


class ReachedHeavyWork(Exception):
    """Raised by the fake as soon as the builder asks for anything past the guard."""


def drive(monkeypatch, tmp_path, target_row, norm=NORM, candidate=CANDIDATE):
    """Run main() with the cloud replaced by a fake, and report which steps it reached."""
    calls: list[str] = []
    out = tmp_path / "candidate_features.json"

    def fake_run(client, sql, label, stats):
        calls.append(label)
        if label == "inspect_target":
            stats.append({"step": label, "bytes_billed": 0, "slot_ms": 0})
            return [dict(target_row)] if target_row is not None else [
                {"n": 0, "fruns": 0, "frun": None, "cruns": 0, "crun": None,
                 "nvers": 0, "nver": None, "fvers": 0, "fver": None}]
        raise ReachedHeavyWork(label)

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "client_as_matcher", lambda: object())
    monkeypatch.setattr(sys, "argv", [
        "build_candidate_features.py", "--norm-version", norm,
        "--candidate-run-id", candidate, "--out", str(out)])
    try:
        mod.main()
        return calls, json.loads(out.read_text())
    except ReachedHeavyWork as reached:
        return calls, reached


def test_an_existing_output_under_the_same_identity_satisfies_the_guard(monkeypatch, tmp_path):
    _calls, report = drive(monkeypatch, tmp_path, PUBLISHED)
    assert isinstance(report, dict), "the guard should have returned before any heavy work"
    assert report["skipped"] is True


def test_the_guard_reaches_no_generation_and_no_write(monkeypatch, tmp_path):
    calls, _report = drive(monkeypatch, tmp_path, PUBLISHED)
    assert calls == ["inspect_target"], f"the builder went past the guard: {calls}"
    for step in ("stage_features", "validate_features", "publish_atomic", "drop_staging",
                 "profile"):
        assert step not in calls


def test_the_guard_returns_the_identity_already_published(monkeypatch, tmp_path):
    _calls, report = drive(monkeypatch, tmp_path, PUBLISHED)
    assert report["feature_run_id"] == EXPECTED_RUN_ID
    assert report["candidate_run_id"] == CANDIDATE
    assert report["normalization_version"] == NORM
    assert report["feature_version"] == F.FEATURE_VERSION
    assert report["feature_rows"] == 3_045_208


def test_an_output_built_from_another_candidate_set_does_not_satisfy_the_guard(
        monkeypatch, tmp_path):
    """A different blocking run derives a different feature id, so the rows published for
    the old one must not be mistaken for this run's output."""
    other = "blk:0000000000000000"
    calls, outcome = drive(
        monkeypatch, tmp_path, {**PUBLISHED, "crun": other}, candidate=other)
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_features" in calls


def test_an_output_built_under_another_normalization_does_not_satisfy_the_guard(
        monkeypatch, tmp_path):
    other = "1.1.0+b3253b155934"
    calls, outcome = drive(monkeypatch, tmp_path, {**PUBLISHED, "nver": other}, norm=other)
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_features" in calls


def test_an_output_under_another_feature_version_does_not_satisfy_the_guard(
        monkeypatch, tmp_path):
    """FEATURE_VERSION is in the run id, so a table written by a different scorer carries
    a different id -- and the version column has to disagree too."""
    calls, outcome = drive(monkeypatch, tmp_path, {**PUBLISHED, "fver": "2.0.0"})
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_features" in calls


@pytest.mark.parametrize("broken", [
    {"n": 3_045_207}, {"n": 3_045_209}, {"n": 0}, {"n": 1},
    {"fruns": 2}, {"cruns": 2}, {"nvers": 2}, {"fvers": 2},
    {"frun": "feat:0000000000000000"},
])
def test_any_mismatched_invariant_does_not_satisfy_the_guard(monkeypatch, tmp_path, broken):
    calls, outcome = drive(monkeypatch, tmp_path, {**PUBLISHED, **broken})
    assert isinstance(outcome, ReachedHeavyWork), f"{broken} wrongly satisfied the guard"
    assert "stage_features" in calls


def test_an_absent_output_does_not_satisfy_the_guard(monkeypatch, tmp_path):
    calls, outcome = drive(monkeypatch, tmp_path, None)
    assert isinstance(outcome, ReachedHeavyWork)
    assert "stage_features" in calls


def test_the_normal_path_is_still_reachable(monkeypatch, tmp_path):
    """When the guard does not fire the builder proceeds exactly as before, starting with
    the feature staging table."""
    _calls, outcome = drive(monkeypatch, tmp_path, {**PUBLISHED, "n": 7})
    assert isinstance(outcome, ReachedHeavyWork)
    assert str(outcome) == "stage_features"


def test_the_feature_run_id_formula_is_unchanged():
    """The identity is derived from inputs alone; changing the formula would orphan
    feat:7411e987640a1051."""
    body = inspect.getsource(mod.main)
    assert 'f"{args.norm_version}|{args.candidate_run_id}|{F.FEATURE_VERSION}"' in body
    assert '"feat:" + hashlib.sha256(' in body
    recomputed = "feat:" + hashlib.sha256(
        f"{NORM}|{CANDIDATE}|{F.FEATURE_VERSION}".encode()).hexdigest()[:16]
    assert recomputed == EXPECTED_RUN_ID


def test_the_ratified_cardinality_is_the_scored_universe():
    """3,045,208 is this table's own grain -- one row per scored (listen, candidate) pair.
    The blocking output's 34,466,312 is the input universe, not this one."""
    assert mod.EXPECTED_PAIRS == 3_045_208
