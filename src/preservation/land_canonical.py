"""Land the canonical snapshot in GCS with a verifiable manifest.

Same discipline as the listens slice: an explicit object, a recorded checksum, and a
committable manifest that carries no data. No wildcard is ever used to address it.

The uploaded artifact is the gzipped CSV extracted from the published tar.zst. Its own
SHA-256 is recorded here because the upstream checksum covers the tar, not the member, so
without this there would be nothing to verify the landed object against.

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
PREFIX = "raw/musicbrainz/canonical/snapshot_date=2026-07-17"

SOURCE = {
    "source_url": (
        "https://data.metabrainz.org/pub/musicbrainz/canonical_data/"
        "musicbrainz-canonical-dump-20260717-080003/"
        "musicbrainz-canonical-dump-20260717-080003.tar.zst"
    ),
    "source_archive_sha256": ("65796cec3609ad45edfcc6a334cb78cae8a4579430bfbe6b9b54c96cf1566cd5"),
    "source_archive_bytes": 2320377487,
    "source_timestamp": "2026-07-17 08:00:03.172114",
    "snapshot_date": "2026-07-17",
    "member": "canonical/canonical_musicbrainz_data.csv",
    "member_bytes_uncompressed": 7519259059,
    "license": "CC0 1.0 Universal (COPYING inside archive)",
    "schema": [
        "id",
        "artist_credit_id",
        "artist_mbids",
        "artist_credit_name",
        "release_mbid",
        "release_name",
        "recording_mbid",
        "recording_name",
        "combined_lookup",
        "score",
    ],
    "measured_grain": "exactly one row per recording_mbid",
    "measured_rows": 31554198,
    "measured_distinct_recording_mbid": 31554198,
}


def hashes(path: str) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), base64.b64encode(md5.digest()).decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()

    from google.cloud import storage

    name = os.path.basename(args.file)
    size = os.path.getsize(args.file)
    sha, md5 = hashes(args.file)

    client = storage.Client()
    bucket = client.bucket(args.bucket)
    bucket.reload()
    cfg = bucket._properties.get("iamConfiguration", {})
    if cfg.get("publicAccessPrevention") != "enforced":
        raise SystemExit("refusing to upload: bucket is not hardened")

    blob_name = f"{PREFIX}/{name}"
    t0 = time.time()
    existing = bucket.get_blob(blob_name)
    if (
        existing is not None
        and existing.size == size
        and (existing.metadata or {}).get("sha256") == sha
    ):
        state, retries = "already_present", 0
        print(f"  {name}: already present and verified")
    else:
        blob = bucket.blob(blob_name)
        blob.metadata = {"sha256": sha, **{k: str(v) for k, v in SOURCE.items() if k != "schema"}}
        blob.content_type = "application/gzip"
        retries = 0
        for attempt in range(4):
            try:
                blob.upload_from_filename(args.file, checksum="md5")
                break
            except Exception as exc:
                retries += 1
                if attempt == 3:
                    raise SystemExit(f"upload failed after retries: {exc}") from exc
                time.sleep(2.0 * (attempt + 1))
        blob.reload()
        if blob.size != size:
            raise SystemExit(f"uploaded size {blob.size} != local {size}")
        if blob.md5_hash != md5:
            raise SystemExit(f"GCS md5 {blob.md5_hash} != local {md5}")
        state = "uploaded"
        print(f"  {name}: {size:,} B uploaded, md5 verified")

    elapsed = time.time() - t0
    manifest = {
        "snapshot_id": "musicbrainz-canonical-20260717",
        **SOURCE,
        "landed_object": f"gs://{args.bucket}/{blob_name}",
        "landed_bytes": size,
        "landed_sha256": sha,
        "landed_md5_base64": md5,
        "compression": "gzip",
        "state": state,
        "retries": retries,
        "seconds": round(elapsed, 1),
    }
    with open(args.manifest, "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "landed_object",
                    "landed_bytes",
                    "landed_sha256",
                    "state",
                    "retries",
                    "seconds",
                )
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
