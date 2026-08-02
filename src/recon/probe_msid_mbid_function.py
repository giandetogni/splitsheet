"""Infer the provenance of recording_mbid from its functional behaviour on recording_msid.

The cross-artifact join is impossible: every retained JSONL incremental was submitted
after the export snapshot, so no single event appears in both. This test substitutes a
structural argument that needs only the preserved slice.

`recording_msid` is ListenBrainz's identifier for a (artist string, track string) pair.
So:

* If recording_mbid were supplied by the submitting client, presence and value would
  depend on *who submitted* rather than on the string. The same msid seen across many
  users would then show BOTH null and non-null values ("mixed"), and sometimes
  conflicting MBIDs.
* If it is derived by ListenBrainz from the string, it is a deterministic function of
  the msid: every occurrence of a given msid carries the same value, and presence is
  all-or-nothing.

A near-zero mixed rate is therefore strong structural evidence consistent with
ListenBrainz-derived mapping. It is not proof: the direct join was impossible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics

MOD = 8  # deterministic 1-in-8 msid keyspace sample keeps this in bounded RAM


def h64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import pyarrow.parquet as pq

    seen: dict[int, list] = {}  # msid_hash -> [rows, non_null, set(mbid_hash)]
    rows_scanned = 0
    files = sorted((f for f in os.listdir(args.slice_dir) if f.endswith(".parquet")),
                   key=lambda f: int(f.split(".")[0]))
    for fname in files:
        tbl = pq.read_table(os.path.join(args.slice_dir, fname),
                            columns=["recording_msid", "recording_mbid"])
        rows_scanned += tbl.num_rows
        for msid, mbid in zip(tbl.column("recording_msid").to_pylist(),
                              tbl.column("recording_mbid").to_pylist()):
            if not isinstance(msid, str):
                continue
            k = h64(msid)
            if k % MOD:
                continue
            slot = seen.get(k)
            if slot is None:
                slot = [0, 0, set()]
                seen[k] = slot
            slot[0] += 1
            if isinstance(mbid, str) and mbid:
                slot[1] += 1
                slot[2].add(h64(mbid))
        print(f"  {fname}: cumulative sampled msids {len(seen):,}")

    repeated = {k: v for k, v in seen.items() if v[0] >= 2}
    mixed = sum(1 for v in repeated.values() if 0 < v[1] < v[0])
    all_null = sum(1 for v in repeated.values() if v[1] == 0)
    all_set = sum(1 for v in repeated.values() if v[1] == v[0])
    conflicting = sum(1 for v in repeated.values() if len(v[2]) > 1)
    occ = sorted(v[0] for v in repeated.values())

    report = {
        "rows_scanned": rows_scanned,
        "msid_sample_fraction": f"1/{MOD}",
        "sampled_distinct_msids": len(seen),
        "sampled_msids_seen_twice_or_more": len(repeated),
        "of_those_all_rows_have_mbid": all_set,
        "of_those_no_rows_have_mbid": all_null,
        "of_those_MIXED_null_and_nonnull": mixed,
        "mixed_pct": round(100 * mixed / max(len(repeated), 1), 4),
        "of_those_with_CONFLICTING_mbids": conflicting,
        "conflicting_pct": round(100 * conflicting / max(len(repeated), 1), 4),
        "occurrences_per_msid_mean": round(statistics.fmean(occ), 3) if occ else 0,
        "occurrences_per_msid_max": occ[-1] if occ else 0,
    }
    strong = report["mixed_pct"] < 1.0 and report["conflicting_pct"] < 1.0
    report["interpretation"] = (
        "strong structural evidence consistent with ListenBrainz-derived mapping: "
        "recording_mbid behaves as a deterministic function of recording_msid, presence "
        "is all-or-nothing per string, and values rarely conflict across users. No direct "
        "proof exists because the event-level join was impossible. The column remains a "
        "ListenBrainz mapper reference label, not ground truth. Mixed and conflicting "
        "cases are candidates for upstream changes; attributing them to mapper revision "
        "requires a temporal analysis ruling out redirects and canonical corrections."
        if strong else
        "recording_mbid varies across occurrences of the same string, which is "
        "consistent with client-supplied values. Treat coverage claims with care."
    )
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print("\n" + json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
