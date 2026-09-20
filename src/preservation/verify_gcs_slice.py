"""Verify the GCS copy of the slice against docs/phase0/period_2026_06_manifest.json.

Checks the cloud copy on its own terms rather than trusting the upload report:

* the bucket is private -- public access prevention enforced, uniform access on, and no
  allUsers / allAuthenticatedUsers in the IAM policy;
* exactly the manifest's members are present, no extras;
* every object's size matches the manifest;
* every object's SHA-256 metadata matches the manifest;
* every object's MD5 as reported by GCS matches the local file, which is what actually
  proves the stored bytes are the same bytes;
* the in-period row count still sums to the figure measured in Phase 0B, read from the
  cloud objects themselves.

The row check reads Parquet footers by ranged GET, and the listened_at column only for
the two boundary members, so it costs single-digit MB of egress instead of 2.67 GB.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os

CHUNK = 8 * 1024 * 1024
PREFIX = "raw/listenbrainz/fullexport-2593/period=2026-06"


class GCSFile(io.RawIOBase):
    """Seekable read-only view of a blob, caching the tail so footer reads cost one GET."""

    def __init__(self, blob, tail: int = 65536) -> None:
        self._blob = blob
        self._size = blob.size
        self._pos = 0
        self.bytes_read = 0
        self._tail_len = min(tail, self._size)
        self._tail_start = self._size - self._tail_len
        self._tail = blob.download_as_bytes(start=self._tail_start, end=self._size - 1)
        self.bytes_read += len(self._tail)

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
            off = self._pos - self._tail_start
            data = self._tail[off : off + n]
        else:
            data = self._blob.download_as_bytes(start=self._pos, end=self._pos + n - 1)
            self.bytes_read += len(data)
        buf[: len(data)] = data
        self._pos += len(data)
        return len(data)


def local_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return base64.b64encode(h.digest()).decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from google.cloud import storage

    with open(args.manifest) as fh:
        man = json.load(fh)
    start = dt.datetime.fromisoformat(man["period_start"])
    end = dt.datetime.fromisoformat(man["period_end_exclusive"])

    client = storage.Client()
    bucket = client.get_bucket(args.bucket)
    cfg = bucket._properties.get("iamConfiguration", {})
    pap = cfg.get("publicAccessPrevention")
    ubla = cfg.get("uniformBucketLevelAccess", {}).get("enabled")

    failures: list[str] = []
    if pap != "enforced":
        failures.append(f"public_access_prevention is {pap}, expected enforced")
    if not ubla:
        failures.append("uniform bucket-level access is not enabled")

    public_members = []
    try:
        policy = bucket.get_iam_policy(requested_policy_version=3)
        for binding in policy.bindings:
            for m in binding.get("members", []):
                if m in ("allUsers", "allAuthenticatedUsers"):
                    public_members.append(f"{m}:{binding.get('role')}")
    except Exception as exc:  # noqa: BLE001 - lacking getIamPolicy must not read as "private"
        failures.append(f"could not read IAM policy, privacy unverified: {exc}")
    if public_members:
        failures.append(f"bucket grants public access: {public_members}")

    expected = {e["member"]: e for e in man["members"]}
    listed = {b.name.rsplit("/", 1)[-1]: b for b in client.list_blobs(bucket, prefix=PREFIX)}
    extras = set(listed) - set(expected) - {"_manifest.json"}
    missing = set(expected) - set(listed)
    if extras:
        failures.append(f"unexpected objects present: {sorted(extras)}")
    if missing:
        failures.append(f"objects missing: {sorted(missing)}")

    egress = 0
    rows_in_period = 0
    per_object = []
    ordered = sorted(expected, key=lambda n: int(n.split(".")[0]))
    boundary = {ordered[0], ordered[-1]}

    for name in ordered:
        if name not in listed:
            continue
        blob = listed[name]
        blob.reload()
        exp = expected[name]
        sha_meta = (blob.metadata or {}).get("sha256")
        md5_here = local_md5(os.path.join(args.raw_dir, name))
        if blob.size != exp["size_bytes"]:
            failures.append(f"{name}: size {blob.size} != manifest {exp['size_bytes']}")
        if sha_meta != exp["sha256"]:
            failures.append(f"{name}: sha256 metadata {sha_meta} != manifest {exp['sha256']}")
        if blob.md5_hash != md5_here:
            failures.append(f"{name}: GCS md5 {blob.md5_hash} != local {md5_here}")

        fh = GCSFile(blob)
        pf = pq.ParquetFile(fh)
        n_rows = pf.metadata.num_rows
        if name in boundary:
            col = pf.read(columns=["listened_at"]).column(0)
            in_period = int(
                pc.sum(pc.and_(pc.greater_equal(col, start), pc.less(col, end))).as_py() or 0
            )
        else:
            # Interior members lie wholly inside the period (Phase 0B contiguity argument).
            in_period = n_rows
        rows_in_period += in_period
        egress += fh.bytes_read
        per_object.append(
            {
                "member": name,
                "size_bytes": blob.size,
                "sha256_metadata_ok": sha_meta == exp["sha256"],
                "md5_matches_local": blob.md5_hash == md5_here,
                "rows": n_rows,
                "rows_in_period": in_period,
                "generation": blob.generation,
            }
        )
        print(
            f"  {name:<14} size ok={blob.size == exp['size_bytes']} "
            f"sha256 ok={sha_meta == exp['sha256']} md5 ok={blob.md5_hash == md5_here} "
            f"rows={n_rows:,} in_period={in_period:,}"
        )

    if rows_in_period != man["expected_rows_in_period"]:
        failures.append(
            f"in-period rows {rows_in_period:,} != " f"manifest {man['expected_rows_in_period']:,}"
        )

    report = {
        "bucket": args.bucket,
        "prefix": PREFIX,
        "bucket_location": bucket.location,
        "bucket_storage_class": bucket.storage_class,
        "public_access_prevention": pap,
        "uniform_bucket_level_access": bool(ubla),
        "public_iam_members": public_members,
        "objects_expected": len(expected),
        "objects_found": len([o for o in per_object]),
        "manifest_object_present": "_manifest.json" in listed,
        "rows_in_period": rows_in_period,
        "expected_rows_in_period": man["expected_rows_in_period"],
        "rows_match": rows_in_period == man["expected_rows_in_period"],
        "all_sha256_metadata_ok": all(o["sha256_metadata_ok"] for o in per_object),
        "all_md5_match_local": all(o["md5_matches_local"] for o in per_object),
        "verification_egress_bytes": egress,
        "failures": failures,
        "objects": per_object,
    }
    with open(args.out, "w") as fh2:
        json.dump(report, fh2, indent=1)

    print(f"\nobjects           : {report['objects_found']}/{report['objects_expected']}")
    print(f"rows in 2026-06   : {rows_in_period:,} (expected {man['expected_rows_in_period']:,})")
    print(f"sha256 metadata   : {report['all_sha256_metadata_ok']}")
    print(f"md5 vs local      : {report['all_md5_match_local']}")
    print(f"public access     : prevention={pap} ubla={bool(ubla)} public_members={public_members}")
    print(f"egress for verify : {egress:,} bytes")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("\nGCS SLICE VERIFIED")


if __name__ == "__main__":
    main()
