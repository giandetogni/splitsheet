"""Upload the preserved 2026-06 slice to the private GCS raw bucket.

This is a preservation copy, not pipeline ingestion: the local disk holding the only
copy is near capacity and the upstream artifact is rotated by MetaBrainz.

Uploads exactly the 23 members named in the manifest plus the manifest itself -- nothing
else, and nothing derived. SHA-256 travels as custom object metadata because GCS itself
stores only MD5 and CRC32C, so the manifest's hash would otherwise not be checkable from
the cloud side.

Idempotent: an object already present with the manifest's size and SHA-256 metadata is
skipped, which matters because the bucket has versioning enabled and a blind re-upload
would create a second version of every object.

Credentials: Application Default Credentials only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time

CHUNK = 8 * 1024 * 1024
PREFIX = "raw/listenbrainz/fullexport-2593/period=2026-06"


def local_hashes(path: str) -> tuple[str, str]:
    """Return (sha256_hex, md5_base64). MD5 is what GCS reports, so it is the wire check."""
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), base64.b64encode(md5.digest()).decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from google.cloud import storage

    with open(args.manifest) as fh:
        man = json.load(fh)

    client = storage.Client()
    bucket = client.bucket(args.bucket)
    bucket.reload()
    iam_cfg = bucket._properties.get("iamConfiguration", {})
    pap = iam_cfg.get("publicAccessPrevention")
    ubla = iam_cfg.get("uniformBucketLevelAccess", {}).get("enabled")
    print(
        f"bucket {args.bucket}: location={bucket.location} class={bucket.storage_class} "
        f"public_access_prevention={pap} ubla={ubla} versioning={bucket.versioning_enabled}"
    )
    if pap != "enforced" or not ubla:
        raise SystemExit("refusing to upload: bucket is not hardened as expected")

    t0 = time.time()
    uploaded = skipped = failures = retries = 0
    bytes_sent = 0
    records = []

    for entry in man["members"]:
        name = entry["member"]
        path = os.path.join(args.raw_dir, name)
        blob_name = f"{PREFIX}/{name}"
        blob = bucket.blob(blob_name)
        sha, md5_b64 = local_hashes(path)
        if sha != entry["sha256"]:
            raise SystemExit(f"{name}: local file no longer matches manifest SHA-256")

        existing = bucket.get_blob(blob_name)
        if (
            existing is not None
            and existing.size == entry["size_bytes"]
            and (existing.metadata or {}).get("sha256") == sha
        ):
            skipped += 1
            records.append(
                {
                    "member": name,
                    "state": "already_present",
                    "size_bytes": existing.size,
                    "sha256": sha,
                    "gcs_md5": existing.md5_hash,
                }
            )
            print(f"  {name:<14} already present, verified")
            continue

        blob.metadata = {
            "sha256": sha,
            "slice_id": man["slice_id"],
            "period": man["period"],
            "source_tar_offset": str(entry["tar_offset"]),
            "source_url": man["source_url"],
            "source_etag": man["source_etag"],
            "license": man["source_license"],
        }
        blob.content_type = "application/octet-stream"
        for attempt in range(4):
            try:
                # checksum="md5" makes the service reject a corrupted transfer instead of
                # storing it silently.
                blob.upload_from_filename(path, checksum="md5", if_generation_match=None)
                break
            except Exception as exc:  # retry any transport-level failure
                retries += 1
                if attempt == 3:
                    failures += 1
                    raise SystemExit(f"{name}: upload failed after retries: {exc}") from exc
                time.sleep(2.0 * (attempt + 1))
        blob.reload()
        if blob.size != entry["size_bytes"]:
            raise SystemExit(f"{name}: uploaded size {blob.size} != manifest {entry['size_bytes']}")
        if blob.md5_hash != md5_b64:
            raise SystemExit(f"{name}: GCS MD5 {blob.md5_hash} != local {md5_b64}")
        uploaded += 1
        bytes_sent += entry["size_bytes"]
        records.append(
            {
                "member": name,
                "state": "uploaded",
                "size_bytes": blob.size,
                "sha256": sha,
                "gcs_md5": blob.md5_hash,
                "gcs_crc32c": blob.crc32c,
                "generation": blob.generation,
            }
        )
        print(f"  {name:<14} {blob.size:>12,}B uploaded, md5 verified")

    # The manifest travels with the data so the bucket is self-describing.
    man_blob = bucket.blob(f"{PREFIX}/_manifest.json")
    man_sha, _ = local_hashes(args.manifest)
    man_blob.metadata = {"sha256": man_sha, "slice_id": man["slice_id"]}
    man_blob.content_type = "application/json"
    man_blob.upload_from_filename(args.manifest, checksum="md5")
    print(f"  _manifest.json uploaded, sha256={man_sha[:16]}…")

    elapsed = time.time() - t0
    report = {
        "bucket": args.bucket,
        "prefix": PREFIX,
        "bucket_location": bucket.location,
        "bucket_storage_class": bucket.storage_class,
        "public_access_prevention": pap,
        "uniform_bucket_level_access": ubla,
        "versioning_enabled": bucket.versioning_enabled,
        "objects_uploaded": uploaded,
        "objects_already_present": skipped,
        "manifest_uploaded": True,
        "bytes_sent": bytes_sent,
        "failures": failures,
        "retries": retries,
        "seconds": round(elapsed, 1),
        "objects": records,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(
        f"\nuploaded={uploaded} skipped={skipped} bytes_sent={bytes_sent:,} "
        f"retries={retries} failures={failures} in {elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
