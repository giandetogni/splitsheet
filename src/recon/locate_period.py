"""Locate a listened_at period inside the indexed full export.

The export is globally sorted by listened_at (established in Phase 0B), so the members
covering a month can be found by binary search instead of scanning. That turns period
selection from a 191 GiB download into a few dozen targeted Range reads.

Reports measured counts, not estimates, for the boundary members it actually reads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from index_fullexport import MemberFile, RangeReader


def month_bounds(period: str) -> tuple[dt.datetime, dt.datetime]:
    year, month = (int(x) for x in period.split("-"))
    # Deliberately tz-naive: pyarrow returns timestamp[ns] as naive datetimes, and
    # ListenBrainz stores listened_at in UTC, so both sides are UTC without tzinfo.
    # Attaching a timezone here would make every comparison raise.
    start = dt.datetime(year, month, 1)  # noqa: DTZ001
    end = dt.datetime(year + (month == 12), (month % 12) + 1, 1)  # noqa: DTZ001
    return start, end


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--period", required=True, help="YYYY-MM")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    start, end = month_bounds(args.period)
    with open(args.index) as fh:
        idx = json.load(fh)
    members = [m for m in idx["members"] if m["name"].endswith(".parquet")]
    members.sort(key=lambda m: int(m["name"].rsplit("/", 1)[-1].split(".")[0]))
    print(f"{len(members)} parquet members; target {args.period} "
          f"[{start.isoformat()}, {end.isoformat()})")

    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    r = RangeReader()
    r.head()
    cache: dict[int, dict] = {}

    def probe(i: int) -> dict:
        """Read only the listened_at column of member i and measure it against the period."""
        if i in cache:
            return cache[i]
        m = members[i]
        q0, b0 = r.requests, r.bytes
        pf = pq.ParquetFile(MemberFile(r, m["offset"], m["size"]))
        col = pf.read(columns=["listened_at"]).column(0)
        lo, hi = pc.min(col).as_py(), pc.max(col).as_py()
        in_range = pc.sum(pc.and_(pc.greater_equal(col, start), pc.less(col, end))).as_py() or 0
        info = {"i": i, "name": m["name"].rsplit("/", 1)[-1], "bytes": m["size"],
                "rows": pf.metadata.num_rows, "ts_min": lo.isoformat(), "ts_max": hi.isoformat(),
                "rows_in_period": int(in_range),
                "read_requests": r.requests - q0, "read_bytes": r.bytes - b0}
        cache[i] = info
        print(f"  probe[{i:>4}] {info['name']:<12} {info['ts_min'][:10]} .. "
              f"{info['ts_max'][:10]}  rows={info['rows']:>9,}  "
              f"in_period={info['rows_in_period']:>8,}  read={info['read_bytes']:>10,}B")
        return info

    # First member whose max reaches into the period.
    lo_i, hi_i = 0, len(members) - 1
    while lo_i < hi_i:
        mid = (lo_i + hi_i) // 2
        if dt.datetime.fromisoformat(probe(mid)["ts_max"]) < start:
            lo_i = mid + 1
        else:
            hi_i = mid
    first = lo_i

    # Last member whose min still falls before the period end.
    lo_j, hi_j = first, len(members) - 1
    while lo_j < hi_j:
        mid = (lo_j + hi_j + 1) // 2
        if dt.datetime.fromisoformat(probe(mid)["ts_min"]) < end:
            lo_j = mid
        else:
            hi_j = mid - 1
    last = lo_j

    covering = list(range(first, last + 1))
    for i in (first, last):
        probe(i)

    # Interior members need no probing. Because the archive is globally sorted and its
    # members are contiguous, any member strictly between `first` and `last` has
    # ts_min >= first.ts_max >= period start and ts_max <= last.ts_min < period end, so
    # every one of its rows lies inside the period. Its num_rows from the index is
    # exact, and reading its data would only re-derive that.
    interior = [i for i in covering if i not in (first, last)]
    if first == last:
        boundary_rows = cache[first]["rows_in_period"]
    else:
        boundary_rows = cache[first]["rows_in_period"] + cache[last]["rows_in_period"]
    interior_rows = sum(members[i].get("rows", 0) for i in interior)
    if interior and not interior_rows:
        # The header walk records bytes, not rows; fetch row counts from footers only.
        import pyarrow.parquet as pq2
        for i in interior:
            m = members[i]
            n = pq2.ParquetFile(MemberFile(r, m["offset"], m["size"])).metadata.num_rows
            members[i]["rows"] = n
            interior_rows += n
    total_bytes = sum(members[i]["size"] for i in covering)

    report = {
        "period": args.period,
        "archive_bytes": idx["archive_bytes"],
        "parquet_members": len(members),
        "temporally_ordered": True,
        "first_member": members[first]["name"].rsplit("/", 1)[-1],
        "last_member": members[last]["name"].rsplit("/", 1)[-1],
        "member_count_covering_period": len(covering),
        "bytes_covering_period": total_bytes,
        "bytes_covering_period_pct_of_archive": round(
            100 * total_bytes / idx["archive_bytes"], 4),
        "rows_in_period_measured": boundary_rows + interior_rows,
        "boundary_rows_counted": boundary_rows,
        "interior_rows_from_footers": interior_rows,
        "interior_member_count": len(interior),
        "probe_count": len(cache),
        "probe_requests": r.requests,
        "probe_bytes": r.bytes,
        "probes": [cache[k] for k in sorted(cache)],
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(f"\nperiod {args.period}: members {report['first_member']}..{report['last_member']} "
          f"({len(covering)} files, {total_bytes:,} B = "
          f"{report['bytes_covering_period_pct_of_archive']}% of archive)")
    print(f"rows in period (measured): {report['rows_in_period_measured']:,}")
    print(f"probes: {len(cache)} members, {r.requests} requests, {r.bytes:,} bytes")
    r.close()


if __name__ == "__main__":
    main()
