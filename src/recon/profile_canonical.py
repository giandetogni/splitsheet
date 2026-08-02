"""Profile canonical_musicbrainz_data.csv read from stdin.

Exists to answer the question that decides whether conservative matching is viable:
for a given normalized artist+recording string, how many distinct recordings does
MusicBrainz actually hold? That count is the ceiling on AMBIGUOUS_TIE, and therefore
on how much revenue conservative matching will suspend.

Uses deterministic 1/MOD keyspace hashing so a 7.5 GB stream profiles in bounded RAM.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import statistics
import sys
from collections import defaultdict

MOD = 64  # sample 1 in 64 of the key space, deterministically


def h(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    csv.field_size_limit(10**9)
    reader = csv.reader(sys.stdin)
    hdr = next(reader)
    col = {name: i for i, name in enumerate(hdr)}

    rows = malformed = blank_cl = nonascii_cl = space_cl = 0
    lookup_recs: dict[int, set[int]] = defaultdict(set)
    rec_sample: set[int] = set()
    score_min = score_max = None

    for row in reader:
        if len(row) != len(hdr):
            malformed += 1
            continue
        rows += 1
        cl = row[col["combined_lookup"]]
        rec = row[col["recording_mbid"]]
        if not cl.strip():
            blank_cl += 1
        else:
            if not cl.isascii():
                nonascii_cl += 1
            if " " in cl:
                space_cl += 1
        hc = h(cl)
        hr = h(rec)
        if hc % MOD == 0:
            lookup_recs[hc].add(hr)
        if hr % MOD == 0:
            rec_sample.add(hr)
        try:
            s = int(row[col["score"]])
            score_min = s if score_min is None else min(score_min, s)
            score_max = s if score_max is None else max(score_max, s)
        except ValueError:
            pass

    sizes = sorted(len(v) for v in lookup_recs.values())

    def pct(p: float) -> int:
        return sizes[min(len(sizes) - 1, int(len(sizes) * p))] if sizes else 0

    amb = sum(1 for s in sizes if s > 1)
    report = {
        "header": hdr,
        "total_rows": rows,
        "malformed_rows": malformed,
        "blank_combined_lookup": blank_cl,
        "non_ascii_combined_lookup": nonascii_cl,
        "non_ascii_combined_lookup_pct": round(100 * nonascii_cl / max(rows, 1), 4),
        "combined_lookup_with_space": space_cl,
        "combined_lookup_with_space_pct": round(100 * space_cl / max(rows, 1), 4),
        "score_min": score_min,
        "score_max": score_max,
        "est_distinct_recording_mbid": len(rec_sample) * MOD,
        "sampled_lookups": len(sizes),
        "est_distinct_combined_lookup": len(sizes) * MOD,
        "recordings_per_lookup": {
            "mean": round(statistics.fmean(sizes), 4) if sizes else 0,
            "p50": pct(0.50), "p90": pct(0.90), "p95": pct(0.95),
            "p99": pct(0.99), "p999": pct(0.999), "max": sizes[-1] if sizes else 0,
        },
        "lookups_with_gt1_recording": amb,
        "lookups_with_gt1_recording_pct": round(100 * amb / max(len(sizes), 1), 4),
    }
    import json
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
