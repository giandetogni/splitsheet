"""Index the ListenBrainz full export over HTTP Range, without downloading it.

The full export is 191 GiB and this machine has ~11 GiB free, so the MVP period cannot
be selected by downloading. Two properties make a cheap index possible: the archive is
an *uncompressed* tar (members are addressable byte ranges) and the server sends
`Accept-Ranges: bytes`. Parquet keeps metadata in a footer, so per member we read tens
of KB instead of ~125 MB.

Modes:
  probe  -- prove the technique on a few members, reporting bytes and requests
  walk   -- index every member's (name, offset, size) into JSON
  stats  -- read footers for selected members, report listened_at min/max
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import time

HOST = "data.metabrainz.org"
PATH = (
    "/pub/musicbrainz/listenbrainz/fullexport/"
    "listenbrainz-dump-2593-20260712-000004-full/"
    "listenbrainz-spark-dump-2593-20260712-000004-full.tar"
)
BLOCK = 512


class RangeReader:
    """Keep-alive HTTPS reader that accounts for every byte and request it makes."""

    def __init__(self, host: str = HOST, path: str = PATH) -> None:
        self.host, self.path = host, path
        self.requests = 0
        self.bytes = 0
        self.total_size: int | None = None
        self._conn: http.client.HTTPSConnection | None = None

    def _connect(self) -> http.client.HTTPSConnection:
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self.host, timeout=60)
        return self._conn

    def head(self) -> int:
        conn = self._connect()
        conn.request("HEAD", self.path)
        r = conn.getresponse()
        r.read()
        if r.getheader("Accept-Ranges") != "bytes":
            raise RuntimeError("server does not advertise byte ranges")
        self.total_size = int(r.getheader("Content-Length"))
        self.requests += 1
        return self.total_size

    def get(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        for attempt in range(3):
            try:
                conn = self._connect()
                conn.request(
                    "GET", self.path, headers={"Range": f"bytes={offset}-{offset + length - 1}"}
                )
                r = conn.getresponse()
                data = r.read()
                if r.status != 206:
                    raise RuntimeError(f"expected 206, got {r.status}")
                self.requests += 1
                self.bytes += len(data)
                return data
            except (http.client.HTTPException, OSError):
                self._conn = None
                if attempt == 2:
                    raise
                time.sleep(1.0 + attempt)
        raise RuntimeError("unreachable")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class MemberFile(io.RawIOBase):
    """Seekable view of one tar member, backed by Range requests.

    Caches the member tail so Parquet footer reads cost one request, not several.
    """

    def __init__(self, reader: RangeReader, offset: int, size: int, tail: int = 65536) -> None:
        self._r, self._off, self._size = reader, offset, size
        self._pos = 0
        self._tail_len = min(tail, size)
        self._tail_start = size - self._tail_len
        self._tail = reader.get(offset + self._tail_start, self._tail_len)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, min(self._size, base + pos))
        return self._pos

    def readinto(self, buf) -> int:  # type: ignore[no-untyped-def]
        n = min(len(buf), self._size - self._pos)
        if n <= 0:
            return 0
        if self._pos >= self._tail_start:
            start = self._pos - self._tail_start
            data = self._tail[start : start + n]
        else:
            data = self._r.get(self._off + self._pos, n)
        buf[: len(data)] = data
        self._pos += len(data)
        return len(data)


def parse_header(block: bytes):
    """Return (name, size, typeflag) for a tar header, or None at end-of-archive."""
    if len(block) < BLOCK or block.strip(b"\0") == b"":
        return None
    if block[257:263] != b"ustar\0":
        raise RuntimeError("not a ustar header")
    name = block[0:100].rstrip(b"\0").decode("utf-8", "replace")
    size = int(block[124:136].rstrip(b"\0 ") or b"0", 8)
    return name, size, block[156:157]


def walk_members(reader: RangeReader, limit: int = 0, chunk: int = 4096):
    """Yield (name, data_offset, size).

    One request per member: the chunk is large enough to cover a pax extended header,
    its payload, and the following real header together.
    """
    total = reader.total_size or reader.head()
    off = 0
    found = 0
    while off < total:
        buf = reader.get(off, min(chunk, total - off))
        cur = 0
        pax_name = None
        while True:
            h = parse_header(buf[cur : cur + BLOCK])
            if h is None:
                return
            name, size, typ = h
            payload = off + cur + BLOCK
            padded = ((size + BLOCK - 1) // BLOCK) * BLOCK
            if typ == b"x":
                blob = buf[cur + BLOCK : cur + BLOCK + size]
                for field in blob.split(b"\n"):
                    if b" path=" in field:
                        pax_name = field.split(b" path=", 1)[1].decode()
                cur += BLOCK + padded
                if cur + BLOCK > len(buf):
                    off = off + cur
                    break
                continue
            yield (pax_name or name), payload, size
            found += 1
            if limit and found >= limit:
                return
            off = payload + padded
            break


def listened_at_stats(mf: MemberFile) -> dict:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(mf)
    md = pf.metadata
    names = [md.schema.column(i).name for i in range(md.num_columns)]
    target = next((n for n in names if n.split(".")[-1] in ("listened_at", "timestamp")), None)
    out = {
        "num_rows": md.num_rows,
        "num_row_groups": md.num_row_groups,
        "columns": len(names),
        "ts_column": target,
    }
    if target is None:
        out["schema_sample"] = names[:30]
        return out
    idx = names.index(target)
    lo = hi = None
    have = True
    for rg in range(md.num_row_groups):
        st = md.row_group(rg).column(idx).statistics
        if st is None or st.min is None:
            have = False
            continue
        lo = st.min if lo is None else min(lo, st.min)
        hi = st.max if hi is None else max(hi, st.max)
    out["has_statistics"] = bool(have and lo is not None)
    if lo is None:
        # Arrow C++ omits min/max statistics for timestamp[ns] columns, so the footer
        # cannot answer the question. Reading the column outright is cheaper than the
        # footer read anyway: listened_at is so well sorted that a 550k-row chunk
        # compresses to a few KB under ZSTD.
        import pyarrow.compute as pc

        col = pf.read(columns=[target]).column(0)
        lo, hi = pc.min(col).as_py(), pc.max(col).as_py()
        out["source"] = "column_read"
    else:
        out["source"] = "footer_statistics"
    if lo is not None:

        def iso(v):
            if hasattr(v, "isoformat"):
                return v.isoformat()
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(v)))

        out["ts_min"], out["ts_max"] = iso(lo), iso(hi)
        out["ts_min_raw"], out["ts_max_raw"] = str(lo), str(hi)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["probe", "walk", "stats"])
    ap.add_argument("--out")
    ap.add_argument("--index")
    ap.add_argument("--members", type=int, default=3)
    ap.add_argument("--pick", default="", help="comma-separated member indices")
    args = ap.parse_args()

    r = RangeReader()
    t0 = time.time()
    size = r.head()
    print(f"archive: {size:,} bytes, Accept-Ranges: bytes")

    if args.mode == "probe":
        members = [
            {"name": n, "offset": o, "size": s}
            for n, o, s in walk_members(r, limit=args.members + 3)
        ]
        big = [m for m in members if m["name"].endswith(".parquet") and m["size"] > 10**6]
        print(f"walked {len(members)} members: {r.requests} requests, {r.bytes:,} bytes")
        for m in big[: args.members]:
            q0, b0 = r.requests, r.bytes
            st = listened_at_stats(MemberFile(r, m["offset"], m["size"]))
            pct = 100 * (r.bytes - b0) / m["size"]
            print(f"\n{m['name']}  size={m['size']:,}")
            print(
                f"  footer: {r.requests - q0} requests, {r.bytes - b0:,} bytes "
                f"({pct:.4f}% of member)"
            )
            print(f"  {json.dumps(st, default=str)}")
        print(f"\nTOTAL {r.requests} requests, {r.bytes:,} bytes, {time.time() - t0:.1f}s")

    elif args.mode == "walk":
        members = [{"name": n, "offset": o, "size": s} for n, o, s in walk_members(r)]
        payload = {
            "archive_bytes": size,
            "member_count": len(members),
            "walk_requests": r.requests,
            "walk_bytes": r.bytes,
            "walk_seconds": round(time.time() - t0, 1),
            "members": members,
        }
        with open(args.out, "w") as fh:
            json.dump(payload, fh, indent=1)
        print(
            f"indexed {len(members)} members: {r.requests} requests, "
            f"{r.bytes:,} bytes, {time.time() - t0:.1f}s -> {args.out}"
        )

    else:
        with open(args.index) as fh:
            idx = json.load(fh)
        members = idx["members"]
        picks = [int(x) for x in args.pick.split(",") if x.strip()]
        results = []
        for i in picks:
            m = members[i]
            q0, b0 = r.requests, r.bytes
            st = listened_at_stats(MemberFile(r, m["offset"], m["size"]))
            st.update(
                {
                    "index": i,
                    "name": m["name"],
                    "member_bytes": m["size"],
                    "footer_bytes": r.bytes - b0,
                    "footer_requests": r.requests - q0,
                }
            )
            results.append(st)
            print(
                json.dumps(
                    {
                        k: st[k]
                        for k in ("index", "name", "num_rows", "ts_min", "ts_max", "footer_bytes")
                        if k in st
                    },
                    default=str,
                )
            )
        out = {
            "stats_requests": r.requests,
            "stats_bytes": r.bytes,
            "stats_seconds": round(time.time() - t0, 1),
            "members": results,
        }
        if args.out:
            with open(args.out, "w") as fh:
                json.dump(out, fh, indent=1)
        print(f"TOTAL {r.requests} requests, {r.bytes:,} bytes, {time.time() - t0:.1f}s")

    r.close()


if __name__ == "__main__":
    main()
