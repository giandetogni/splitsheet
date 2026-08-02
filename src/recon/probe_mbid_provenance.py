"""Test whether the Parquet export's recording_mbid is client-supplied or LB-derived.

Joins equivalent events across the two artifacts on (user_id, listened_at,
recording_msid), which both carry, and compares the MBID each side reports.

The decisive statistic is `parquet_has_mbid_client_did_not`: if the Parquet export
reports a recording_mbid for an event where the JSONL shows no client-supplied MBID,
that value cannot have come from the submitting client and must have been derived by
ListenBrainz. Until this count is known, the column's provenance is inference only, and
it must be described as a "ListenBrainz mapper reference label", never ground truth.

Reads the JSONL side from stdin; the Parquet side from the preserved slice.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
from collections import Counter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice-dir", required=True)
    ap.add_argument("--period", required=True, help="YYYY-MM to keep from the JSONL side")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import pyarrow.parquet as pq

    c: Counter = Counter()
    events: dict[tuple[int, int, str], dict] = {}
    for line in sys.stdin:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            c["jsonl_parse_errors"] += 1
            continue
        ts = rec.get("timestamp")
        if not isinstance(ts, int):
            continue
        if time.strftime("%Y-%m", time.gmtime(ts)) != args.period:
            continue
        c["jsonl_events_in_period"] += 1
        uid, msid = rec.get("user_id"), rec.get("recording_msid")
        if not isinstance(uid, int) or not isinstance(msid, str):
            continue
        tm = rec.get("track_metadata") or {}
        ai = tm.get("additional_info") or {}
        mm = tm.get("mbid_mapping") or {}
        events[(uid, ts, msid)] = {
            "client_mbid": ai.get("recording_mbid"),
            "mapper_mbid": mm.get("recording_mbid"),
        }
    c["jsonl_join_keys"] = len(events)
    print(f"JSONL side: {c['jsonl_events_in_period']:,} events in {args.period}, "
          f"{len(events):,} join keys")

    files = sorted((f for f in os.listdir(args.slice_dir) if f.endswith(".parquet")),
                   key=lambda f: int(f.split(".")[0]))
    matches = []
    for fname in files:
        tbl = pq.read_table(os.path.join(args.slice_dir, fname),
                            columns=["user_id", "listened_at", "recording_msid",
                                     "recording_mbid"])
        c["parquet_rows_scanned"] += tbl.num_rows
        uids = tbl.column("user_id").to_pylist()
        tss = tbl.column("listened_at").to_pylist()
        msids = tbl.column("recording_msid").to_pylist()
        mbids = tbl.column("recording_mbid").to_pylist()
        for uid, ts, msid, mbid in zip(uids, tss, msids, mbids):
            key = (uid, calendar.timegm(ts.utctimetuple()), msid)
            hit = events.get(key)
            if hit is not None:
                matches.append({"client_mbid": hit["client_mbid"],
                                "mapper_mbid": hit["mapper_mbid"],
                                "parquet_mbid": mbid})
        print(f"  scanned {fname} ({tbl.num_rows:,} rows), matches so far: {len(matches):,}")

    c["matched_events"] = len(matches)
    for m in matches:
        pq_has = isinstance(m["parquet_mbid"], str) and bool(m["parquet_mbid"])
        cl_has = isinstance(m["client_mbid"], str) and bool(m["client_mbid"])
        if pq_has and not cl_has:
            c["parquet_has_mbid_client_did_not"] += 1
        if pq_has and cl_has:
            c["both_have_mbid"] += 1
            c["client_agrees_with_parquet"] += int(
                m["client_mbid"].lower() == m["parquet_mbid"].lower())
        if not pq_has and cl_has:
            c["client_has_mbid_parquet_did_not"] += 1
        if not pq_has and not cl_has:
            c["neither_has_mbid"] += 1
        if isinstance(m["mapper_mbid"], str) and pq_has:
            c["mapper_label_present_both"] += 1
            c["mapper_agrees_with_parquet"] += int(
                m["mapper_mbid"].lower() == m["parquet_mbid"].lower())

    verdict = "inconclusive: no overlapping events between artifacts"
    if c["matched_events"]:
        if c["parquet_has_mbid_client_did_not"] > 0:
            verdict = ("strong evidence consistent with ListenBrainz-derived mapping: "
                       "Parquet reports MBIDs where no client MBID exists")
        else:
            verdict = ("no derived MBIDs observed in the matched sample; consistent with "
                       "client-supplied only")
    report = {"period": args.period, "counts": dict(c), "verdict": verdict}
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print("\n" + json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
