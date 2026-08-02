"""Normalize each distinct (artist, recording) pair once, not each listen.

Measured on the June corpus: 38,199,641 listens carry only 4,599,791 distinct pairs, so
normalizing per distinct pair is 8.3x less work. At the measured 22.8 us per pair that is
about 105 seconds instead of roughly 14.5 minutes, and it keeps the rules in one Python
implementation instead of pushing them into SQL.

The result is a mapping table joined onto listens in BigQuery on `pair_hash`.

`pair_hash` is SHA-256 of the two raw strings joined by U+001F. It is an identity function
over the inputs, not a normalization rule, so computing the identical expression in SQL
creates no second implementation of anything.

Reads the preserved local Parquet slice, applying the SAME half-open period filter that
v_matcher_input applies. The slice holds 39,200,000 rows across 23 members, of which only
38,199,641 are in period; reading it unfiltered silently widens the corpus. The
distinct-pair count is asserted against the figure measured in BigQuery, which is what
caught exactly that mistake. No GCP call.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from normalization import load_rules, normalize

SEP = "\x1f"
EXPECTED_DISTINCT_PAIRS = 4_599_791
EXPECTED_LISTENS = 38_199_641

# Half-open, identical to v_matcher_input. tz-naive because pyarrow returns timestamp[ns]
# as naive datetimes and ListenBrainz stores listened_at in UTC.
PERIOD_START = dt.datetime(2026, 6, 1)  # noqa: DTZ001
PERIOD_END = dt.datetime(2026, 7, 1)  # noqa: DTZ001

COLUMNS = [
    "pair_hash", "artist_name", "recording_name",
    "artist_normalized_unicode", "recording_normalized_unicode",
    "lookup_exact", "lookup_fallback",
    "normalization_status", "exact_key_status", "fallback_key_status",
]


def pair_hash(artist: str, recording: str) -> str:
    return hashlib.sha256(f"{artist}{SEP}{recording}".encode()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stats", required=True)
    args = ap.parse_args()

    import pyarrow.parquet as pq

    rules = load_rules()
    seen: set[str] = set()
    c: Counter = Counter()
    t0 = time.time()

    files = sorted((f for f in os.listdir(args.slice_dir) if f.endswith(".parquet")),
                   key=lambda f: int(f.split(".")[0]))
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n",
                       quoting=csv.QUOTE_MINIMAL)
        for fname in files:
            tbl = pq.read_table(os.path.join(args.slice_dir, fname),
                                columns=["artist_name", "recording_name", "listened_at"])
            c["rows_in_member"] += tbl.num_rows
            for artist, recording, ts in zip(tbl.column("artist_name").to_pylist(),
                                             tbl.column("recording_name").to_pylist(),
                                             tbl.column("listened_at").to_pylist()):
                if ts is None or ts < PERIOD_START or ts >= PERIOD_END:
                    c["rows_outside_period"] += 1
                    continue
                c["listens"] += 1
                a, r = artist or "", recording or ""
                ph = pair_hash(a, r)
                if ph in seen:
                    continue
                seen.add(ph)
                n = normalize(a, r, rules=rules)
                c[f"norm_{n.normalization_status.value}"] += 1
                c[f"exact_{n.exact_key_status.value}"] += 1
                c[f"fallback_{n.fallback_key_status.value}"] += 1
                w.writerow([
                    ph, a, r,
                    n.artist_normalized_unicode, n.recording_normalized_unicode,
                    n.lookup_exact, n.lookup_fallback,
                    n.normalization_status.value,
                    n.exact_key_status.value, n.fallback_key_status.value,
                ])
            print(f"  {fname}: distinct pairs so far {len(seen):,}", flush=True)

    c["distinct_pairs"] = len(seen)
    problems = []
    if c["listens"] != EXPECTED_LISTENS:
        problems.append(f"listens {c['listens']} != {EXPECTED_LISTENS}")
    if c["distinct_pairs"] != EXPECTED_DISTINCT_PAIRS:
        problems.append(
            f"distinct pairs {c['distinct_pairs']} != BigQuery's {EXPECTED_DISTINCT_PAIRS}")

    stats = {
        "columns": COLUMNS,
        "counts": dict(c),
        "normalization_version": rules.version,
        "normalization_rules_sha256": rules.rules_digest,
        "reduction_factor": round(c["listens"] / max(c["distinct_pairs"], 1), 2),
        "seconds": round(time.time() - t0, 1),
        "problems": problems,
    }
    with open(args.stats, "w") as fh:
        json.dump(stats, fh, indent=1)
    print(json.dumps(stats, indent=1))
    if problems:
        raise SystemExit("mapping build disagrees with the BigQuery measurement")


if __name__ == "__main__":
    main()
