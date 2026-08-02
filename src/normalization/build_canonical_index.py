"""Build the canonical blocking index using the production normalization library.

The index is generated in Python, not SQL, because the normalization rules have exactly
one implementation and re-expressing them in SQL would create a second one that drifts.

Grain, explicit and enforced downstream:

    recording_mbid + lookup_stage + lookup_key

One row per stage, because the blocking join is per-stage: the exact stage must never see
a fallback key. A recording contributes an EXACT row and a FALLBACK row even when the two
keys are identical, since the stage is part of the identity of the row.

Rows whose key is empty are omitted entirely: a recording with no ASCII key cannot be
found by an ASCII key, and emitting a blank key would create a bucket that matches
everything unmatchable.

Output is gzipped TSV, which BigQuery loads directly. Runs on a local file, no GCP call.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from normalization import load_rules, normalize


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="gzipped TSV destination")
    ap.add_argument("--stats", required=True)
    args = ap.parse_args()

    rules = load_rules()
    csv.field_size_limit(10**9)
    reader = csv.reader(sys.stdin)
    hdr = next(reader)
    col = {n: i for i, n in enumerate(hdr)}
    ia, it, ir = col["artist_credit_name"], col["recording_name"], col["recording_mbid"]

    c: Counter = Counter()
    t0 = time.time()
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as out:
        w = csv.writer(out, delimiter="\t", lineterminator="\n")
        for row in reader:
            if len(row) != len(hdr):
                c["malformed"] += 1
                continue
            c["canonical_rows"] += 1
            mbid = row[ir]
            if not mbid:
                c["rows_without_mbid"] += 1
                continue
            n = normalize(row[ia], row[it], rules=rules)
            c[f"exact_{n.exact_key_status.value}"] += 1
            c[f"fallback_{n.fallback_key_status.value}"] += 1
            if n.lookup_exact:
                w.writerow([mbid, "EXACT", n.lookup_exact])
                c["index_rows_exact"] += 1
            if n.lookup_fallback:
                w.writerow([mbid, "FALLBACK", n.lookup_fallback])
                c["index_rows_fallback"] += 1
            if n.lookup_exact and n.lookup_exact == n.lookup_fallback:
                c["stages_identical"] += 1
            if c["canonical_rows"] % 5_000_000 == 0:
                print(f"  {c['canonical_rows']:,} rows -> "
                      f"{c['index_rows_exact'] + c['index_rows_fallback']:,} index rows",
                      flush=True)

    stats = {
        "normalization_version": rules.version,
        "counts": dict(c),
        "index_rows_total": c["index_rows_exact"] + c["index_rows_fallback"],
        "recordings_with_no_ascii_key_pct": round(
            100 * c["exact_EMPTY"] / max(c["canonical_rows"], 1), 4),
        "seconds": round(time.time() - t0, 1),
    }
    with open(args.stats, "w") as fh:
        json.dump(stats, fh, indent=1)
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
