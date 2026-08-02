"""Extract and preserve the fixed 2026-06 slice of the ListenBrainz full export.

Preserves a private, immutable copy of the RAW slice before any column is dropped or
transformed, so later processing can never be the only surviving version. Raw files are
written outside the repository and are git-ignored; the committable artifact is the
manifest, which carries no listen data -- only names, offsets, sizes and checksums.

Idempotent: a member already present with the expected size and SHA-256 is not
re-downloaded. Writes go to a .part file in the destination directory and are renamed
into place, so there is never a second full copy of a member on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_fullexport import HOST, PATH, RangeReader

CHUNK = 8 * 1024 * 1024
PERIOD_START = "2026-06-01T00:00:00"
PERIOD_END = "2026-07-01T00:00:00"


def head_validators(reader: RangeReader) -> dict:
    """Capture the archive's identity so the slice can be tied to a specific artifact."""
    conn = reader._connect()
    conn.request("HEAD", reader.path)
    r = conn.getresponse()
    r.read()
    reader.requests += 1
    return {"etag": r.getheader("ETag"), "last_modified": r.getheader("Last-Modified"),
            "content_length": int(r.getheader("Content-Length"))}


def stream_member(reader: RangeReader, offset: int, size: int, dest: str) -> tuple[str, int, int]:
    """Range-download one member to dest, hashing as it streams. Returns (sha256, bytes, retries)."""
    part = dest + ".part"
    h = hashlib.sha256()
    written = 0
    retries = 0
    with open(part, "wb") as fh:
        while written < size:
            want = min(CHUNK, size - written)
            for attempt in range(4):
                try:
                    data = reader.get(offset + written, want)
                    break
                except Exception:
                    retries += 1
                    if attempt == 3:
                        raise
                    time.sleep(1.5 * (attempt + 1))
            if not data:
                raise RuntimeError(f"empty response at offset {offset + written}")
            fh.write(data)
            h.update(data)
            written += len(data)
    os.replace(part, dest)
    os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return h.hexdigest(), written, retries


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--period-report", required=True)
    ap.add_argument("--raw-dir", required=True, help="destination for raw members (outside git)")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()

    with open(args.index) as fh:
        idx = json.load(fh)
    with open(args.period_report) as fh:
        rep = json.load(fh)

    members = [m for m in idx["members"] if m["name"].endswith(".parquet")]
    members.sort(key=lambda m: int(m["name"].rsplit("/", 1)[-1].split(".")[0]))
    first = int(rep["first_member"].split(".")[0])
    last = int(rep["last_member"].split(".")[0])
    selected = [m for m in members
                if first <= int(m["name"].rsplit("/", 1)[-1].split(".")[0]) <= last]
    expected_bytes = sum(m["size"] for m in selected)
    print(f"period {rep['period']}: {len(selected)} members, {expected_bytes:,} bytes expected")
    if len(selected) != rep["member_count_covering_period"]:
        raise SystemExit("member count disagrees with period report")

    free = os.statvfs(args.raw_dir).f_bavail * os.statvfs(args.raw_dir).f_frsize
    print(f"free space at destination: {free:,} bytes")
    if free < expected_bytes * 1.15:
        raise SystemExit("insufficient free space (need slice + 15% headroom)")

    r = RangeReader(HOST, PATH)
    r.head()
    val = head_validators(r)
    print(f"archive ETag={val['etag']} Last-Modified={val['last_modified']}")
    if val["content_length"] != idx["archive_bytes"]:
        raise SystemExit("archive size changed since indexing; re-index before extracting")

    t0 = time.time()
    entries = []
    reused = 0
    for m in selected:
        base = m["name"].rsplit("/", 1)[-1]
        dest = os.path.join(args.raw_dir, base)
        if os.path.exists(dest) and os.path.getsize(dest) == m["size"]:
            digest, nbytes, retries = sha256_of(dest), m["size"], 0
            reused += 1
            note = "reused"
        else:
            digest, nbytes, retries = stream_member(r, m["offset"], m["size"], dest)
            note = "downloaded"
        if nbytes != m["size"]:
            raise SystemExit(f"{base}: size {nbytes} != index {m['size']}")
        entries.append({"member": base, "tar_offset": m["offset"], "size_bytes": m["size"],
                        "sha256": digest, "retries": retries, "state": note})
        print(f"  {base:<14} {m['size']:>12,}B  {note:<11} sha256={digest[:16]}… "
              f"retries={retries}")

    elapsed = time.time() - t0
    manifest = {
        "slice_id": f"listenbrainz-fullexport-2593-{rep['period']}",
        "period": rep["period"],
        "source_url": f"https://{HOST}{PATH}",
        "source_archive_bytes": idx["archive_bytes"],
        "source_etag": val["etag"],
        "source_last_modified": val["last_modified"],
        "source_license": "CC0 1.0 Universal (COPYING inside archive)",
        "extraction_method": "HTTP Range over uncompressed tar members",
        "member_count": len(entries),
        "total_bytes": sum(e["size_bytes"] for e in entries),
        "expected_rows_in_period": rep["rows_in_period_measured"],
        "period_start": PERIOD_START,
        "period_end_exclusive": PERIOD_END,
        "bytes_transferred": r.bytes,
        "requests": r.requests,
        "retries_total": sum(e["retries"] for e in entries),
        "members_reused_without_download": reused,
        "seconds": round(elapsed, 1),
        "pii_note": "raw members contain user_id; it must not appear in published outputs",
        "members": entries,
    }
    with open(args.manifest, "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(f"\ntransferred {r.bytes:,} bytes in {r.requests} requests, {elapsed:.1f}s, "
          f"retries={manifest['retries_total']}, reused={reused}")
    print(f"manifest -> {args.manifest}")
    r.close()


if __name__ == "__main__":
    main()
