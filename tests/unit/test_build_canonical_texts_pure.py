"""Unit tests for the canonical text builder's pure logic. No GCP, no credentials, no cost.

The risk these cover is the one that actually bit: a rerun on unchanged inputs regenerating
the payload and minting a new `ingestion_run_id` for identical content, which the spec
forbids ("produzir chaves diferentes sem mudança de versão").
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import inspect
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from normalization.build_canonical_texts import (
    SNAPSHOT,
    main,
    published_identity,
    source_rows,
)

UNIVERSE = "blk:c005e9a56b1ec542"
PUBLISHED = {
    "n": 368795, "runs": 1, "run_id": "cantext:8a38e905f9d3577d",
    "versions": 1, "version": "1.0.0+0bc0dd643e06", "rules_digest": "0bc0dd643e06",
    "universes": 1, "universe": UNIVERSE, "snapshots": 1, "snapshot": SNAPSHOT,
}


class FakeRules:
    def __init__(self, version="1.0.0+0bc0dd643e06", rules_digest="0bc0dd643e06"):
        self.version = version
        self.rules_digest = rules_digest


class FakeClient:
    """Records every query it is asked to run, so 'did not touch anything' is provable."""

    def __init__(self, row):
        self._row = row
        self.queries: list[str] = []

    def query(self, sql, **_):
        self.queries.append(sql)
        row = self._row

        class _Job:
            def result(self):
                return iter([row])
        return _Job()


def test_guard_returns_the_identity_already_published_for_these_inputs():
    client = FakeClient(PUBLISHED)
    found = published_identity(client, FakeRules(), UNIVERSE)
    assert found is not None
    assert found["run_id"] == "cantext:8a38e905f9d3577d"


def test_guard_only_reads_and_never_writes():
    client = FakeClient(PUBLISHED)
    published_identity(client, FakeRules(), UNIVERSE)
    assert len(client.queries) == 1
    sql = client.queries[0].upper()
    for statement in ("INSERT", "DELETE", "UPDATE", "MERGE", "CREATE", "DROP", "TRUNCATE"):
        assert statement not in sql, f"the guard emitted {statement}"
    assert "SELECT" in sql


def test_a_different_normalization_version_does_not_satisfy_the_guard():
    client = FakeClient(PUBLISHED)
    assert published_identity(client, FakeRules(version="1.1.0+b3253b155934"), UNIVERSE) is None


def test_a_different_rules_digest_does_not_satisfy_the_guard():
    client = FakeClient(PUBLISHED)
    assert published_identity(client, FakeRules(rules_digest="deadbeef"), UNIVERSE) is None


def test_a_different_candidate_universe_does_not_satisfy_the_guard():
    client = FakeClient(PUBLISHED)
    assert published_identity(client, FakeRules(), "blk:0000000000000000") is None


def test_an_empty_or_mixed_target_does_not_satisfy_the_guard():
    for bad in ({**PUBLISHED, "n": 0}, {**PUBLISHED, "runs": 2}, {**PUBLISHED, "versions": 2},
                {**PUBLISHED, "universes": 2}, {**PUBLISHED, "snapshots": 2},
                {**PUBLISHED, "snapshot": "2025-01-01"}):
        assert published_identity(FakeClient(bad), FakeRules(), UNIVERSE) is None


def test_the_source_query_is_deterministically_ordered():
    body = inspect.getsource(source_rows)
    assert "ORDER BY k.recording_mbid" in body, "rebuild must not depend on BigQuery row order"


def _serialise(rows):
    """The builder's framing: deterministic gzip over a tab-separated payload."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        for r in rows:
            w.writerow(r)
    return buf.getvalue()


ROWS = [["b", "artist b", "rec b", "rel b"], ["a", "artist a", "rec a", ""]]


def test_two_serialisations_of_the_same_payload_are_byte_identical():
    first, second = _serialise(sorted(ROWS)), _serialise(sorted(ROWS))
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert first == second


def test_the_gzip_header_carries_no_timestamp():
    blob = _serialise(sorted(ROWS))
    mtime = int.from_bytes(blob[4:8], "little")
    assert mtime == 0, "a timestamp in the header would change the digest, and so the run id"


def test_the_run_id_formula_is_unchanged():
    """Guarding the formula itself: changing it would recompute every published identity,
    including cantext:8a38e905f9d3577d."""
    body = inspect.getsource(main)
    assert 'digest = hashlib.sha256(local.read_bytes()).hexdigest()' in body
    assert ('f"{rules.version}|{rules.rules_digest}|{args.candidate_run_id}|{n}|{digest}"'
            in body)
    assert '"cantext:" + hashlib.sha256(' in body
