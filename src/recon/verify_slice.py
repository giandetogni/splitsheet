"""Verify the preserved 2026-06 slice against the manifest and the period measurement.

Checks three independent things, so a silent corruption or a wrong member selection
cannot pass: byte size per member against the tar index, SHA-256 per member against the
manifest recorded at download time, and the exact in-period row count summed across
members against the figure measured during period location.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os

CHUNK = 8 * 1024 * 1024


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    with open(args.manifest) as fh:
        man = json.load(fh)
    start = dt.datetime.fromisoformat(man["period_start"])
    end = dt.datetime.fromisoformat(man["period_end_exclusive"])

    rows_total = in_period_total = 0
    failures: list[str] = []
    per_member = []
    prev_max = None

    for e in man["members"]:
        path = os.path.join(args.raw_dir, e["member"])
        size = os.path.getsize(path)
        digest = sha256_of(path)
        pf = pq.ParquetFile(path)
        n = pf.metadata.num_rows
        col = pf.read(columns=["listened_at"]).column(0)
        lo, hi = pc.min(col).as_py(), pc.max(col).as_py()
        in_period = int(pc.sum(pc.and_(pc.greater_equal(col, start),
                                       pc.less(col, end))).as_py() or 0)
        writable = os.access(path, os.W_OK)

        if size != e["size_bytes"]:
            failures.append(f"{e['member']}: size {size} != manifest {e['size_bytes']}")
        if digest != e["sha256"]:
            failures.append(f"{e['member']}: sha256 mismatch")
        if writable:
            failures.append(f"{e['member']}: file is writable, expected read-only")
        if prev_max is not None and lo < prev_max:
            failures.append(f"{e['member']}: listened_at overlaps previous member")
        prev_max = hi

        rows_total += n
        in_period_total += in_period
        per_member.append({"member": e["member"], "size_bytes": size,
                           "sha256_ok": digest == e["sha256"], "read_only": not writable,
                           "rows": n, "rows_in_period": in_period,
                           "ts_min": lo.isoformat(), "ts_max": hi.isoformat()})
        print(f"  {e['member']:<14} rows={n:>9,} in_period={in_period:>9,} "
              f"{lo.date()}..{hi.date()} sha256={'ok' if digest == e['sha256'] else 'BAD'}")

    expected = man["expected_rows_in_period"]
    if in_period_total != expected:
        failures.append(f"in-period rows {in_period_total:,} != measured {expected:,}")

    report = {
        "slice_id": man["slice_id"],
        "member_count": len(per_member),
        "total_bytes": sum(m["size_bytes"] for m in per_member),
        "rows_all_members": rows_total,
        "rows_in_period": in_period_total,
        "expected_rows_in_period": expected,
        "rows_match": in_period_total == expected,
        "all_sha256_ok": all(m["sha256_ok"] for m in per_member),
        "all_read_only": all(m["read_only"] for m in per_member),
        "temporally_contiguous": not any("overlaps" in f for f in failures),
        "failures": failures,
        "members": per_member,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)

    print(f"\nrows across all members : {rows_total:,}")
    print(f"rows inside 2026-06     : {in_period_total:,}")
    print(f"expected (measured)     : {expected:,}")
    print(f"sha256 all ok           : {report['all_sha256_ok']}")
    print(f"all read-only           : {report['all_read_only']}")
    print(f"temporally contiguous   : {report['temporally_contiguous']}")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("\nSLICE VERIFIED")


if __name__ == "__main__":
    main()
