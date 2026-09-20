"""Measure the canonical file's real grain before any table is defined for it.

Phase 0A estimated, from a 1-in-64 sample, that there is one row per recording_mbid. An
estimate is not a grain. If the file actually carries semantically distinct variants per
recording, forcing one row per recording_mbid would silently discard them, and the loss
would only surface much later as unexplained missing candidates.

Two passes over the CSV, because duplicates cannot be characterised until they are known:
  pass A  identify which recording_mbids appear more than once
  pass B  collect the differing field values for only those

Runs on a local file. No GCP call.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter

BLOCKING_FIELDS = ("artist_credit_name", "recording_name")


def h(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="phase", choices=["a", "b"], required=True)
    ap.add_argument("--dupes", help="pass A output, required for pass B")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    csv.field_size_limit(10**9)
    reader = csv.reader(sys.stdin)
    hdr = next(reader)
    col = {n: i for i, n in enumerate(hdr)}
    i_mbid = col["recording_mbid"]

    if args.phase == "a":
        c: Counter = Counter()
        seen: set[int] = set()
        repeated: set[int] = set()
        row_hashes: set[int] = set()

        for row in reader:
            if len(row) != len(hdr):
                c["malformed_rows"] += 1
                continue
            c["source_rows"] += 1
            mbid = row[i_mbid]
            if not mbid.strip():
                c["rows_without_recording_mbid"] += 1
            else:
                k = h(mbid)
                if k in seen:
                    repeated.add(k)
                else:
                    seen.add(k)
            rh = h("\x1f".join(row))
            if rh in row_hashes:
                c["exact_duplicate_rows"] += 1
            else:
                row_hashes.add(rh)
            if row[col["combined_lookup"]].strip():
                c["combined_lookup_present"] += 1
            if all(row[col[f]].strip() for f in BLOCKING_FIELDS):
                c["blocking_fields_present"] += 1

        out = {
            "header": hdr,
            "counts": dict(c),
            "distinct_recording_mbid": len(seen),
            "recording_mbids_with_multiple_rows": len(repeated),
            "excess_rows_over_distinct_mbid": c["source_rows"]
            - len(seen)
            - c["rows_without_recording_mbid"],
            "combined_lookup_coverage_pct": round(
                100 * c["combined_lookup_present"] / max(c["source_rows"], 1), 4
            ),
            "blocking_fields_coverage_pct": round(
                100 * c["blocking_fields_present"] / max(c["source_rows"], 1), 4
            ),
            "_repeated_hashes": sorted(repeated),
        }
        with open(args.out, "w") as fh:
            json.dump(out, fh)
        summary = {k: v for k, v in out.items() if k not in ("_repeated_hashes", "header")}
        print(json.dumps(summary, indent=1))
        return

    # pass B: characterise the duplicated recordings only
    with open(args.dupes) as fh:
        repeated = set(json.load(fh)["_repeated_hashes"])
    variants: dict[int, dict[str, set]] = {}
    for row in reader:
        if len(row) != len(hdr):
            continue
        k = h(row[i_mbid])
        if k not in repeated:
            continue
        v = variants.setdefault(
            k,
            {"artist": set(), "recording": set(), "release": set(), "lookup": set(), "rows": set()},
        )
        v["artist"].add(row[col["artist_credit_name"]])
        v["recording"].add(row[col["recording_name"]])
        v["release"].add(row[col["release_name"]])
        v["lookup"].add(row[col["combined_lookup"]])
        v["rows"].add(h("\x1f".join(row)))

    dist: Counter = Counter()
    differing: Counter = Counter()
    for v in variants.values():
        dist[len(v["rows"])] += 1
        for field in ("artist", "recording", "release", "lookup"):
            if len(v[field]) > 1:
                differing[field] += 1
    out = {
        "recording_mbids_examined": len(variants),
        "rows_per_recording_mbid_distribution": dict(sorted(dist.items())),
        "recording_mbids_with_differing_values": dict(differing),
        "interpretation": (
            "If differing values are zero, the extra rows are exact duplicates and the "
            "grain is one row per recording_mbid. If any field differs, the file carries "
            "variants and collapsing to one row per recording_mbid would discard them."
        ),
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
