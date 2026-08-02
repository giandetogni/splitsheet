"""Unit tests for the loader's pure logic. No GCP, no credentials, no cost."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src/ingestion"))
from load_period import (
    ALLOWED_BRONZE_COLUMNS,
    BANNED_COLUMNS,
    EXPECTED_IN_PERIOD,
    EXPECTED_OUTSIDE,
    EXPECTED_SOURCE_TOTAL,
    run_id_from_manifest,
)

MANIFEST = pathlib.Path(__file__).parents[2] / "docs/phase0/period_2026_06_manifest.json"


def test_reconciliation_constants_are_self_consistent():
    """The whole ingestion contract rests on these three numbers adding up."""
    assert EXPECTED_IN_PERIOD + EXPECTED_OUTSIDE == EXPECTED_SOURCE_TOTAL


def test_allowed_and_banned_columns_are_disjoint():
    assert not (set(ALLOWED_BRONZE_COLUMNS) & BANNED_COLUMNS)


def test_run_id_is_deterministic_for_the_same_manifest():
    """Idempotency depends on this: same slice content must yield the same run id."""
    with open(MANIFEST) as fh:
        man = json.load(fh)
    assert run_id_from_manifest(man) == run_id_from_manifest(man)
    assert run_id_from_manifest(man).startswith(man["slice_id"] + ":")


def test_run_id_changes_when_slice_content_changes():
    """A different member checksum must produce a different run id, or a changed slice
    would be published under the identity of the old one."""
    with open(MANIFEST) as fh:
        man = json.load(fh)
    before = run_id_from_manifest(man)
    tampered = json.loads(json.dumps(man))
    tampered["members"][0]["sha256"] = "0" * 64
    assert run_id_from_manifest(tampered) != before
