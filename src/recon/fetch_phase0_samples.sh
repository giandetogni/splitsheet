#!/usr/bin/env bash
# Fetch the pinned Phase 0 recon artifacts and verify them against the published
# SHA-256. Pinned by exact filename because MetaBrainz deletes old incremental dumps
# (~12 day retention) -- if this script starts failing on the listens dump, that is
# retention, not a bug, and the recon must be re-pinned to a current dump.
set -euo pipefail

BASE="https://data.metabrainz.org/pub/musicbrainz"
DATA_DIR="${DATA_DIR:-data/raw}"

LISTENS_DIR="listenbrainz/incremental/listenbrainz-dump-2611-20260730-000002-incremental"
LISTENS_FILE="listenbrainz-listens-dump-2611-20260730-000002-incremental.tar.zst"
CANON_DIR="canonical_data/musicbrainz-canonical-dump-20260717-080003"
CANON_FILE="musicbrainz-canonical-dump-20260717-080003.tar.zst"

mkdir -p "$DATA_DIR"

fetch() {
  local url="$1" out="$2"
  if [[ -f "$out" ]]; then
    echo "present: $out"
  else
    echo "downloading: $url"
    curl -fSL --retry 3 "$url" -o "$out.part"
    mv "$out.part" "$out"
  fi
  echo "verifying sha256: $out"
  curl -fsSL "$url.sha256" -o "$out.sha256"
  # Published file is "<hash>  <name>"; compare hashes only, names differ by path.
  local want got
  want="$(awk '{print $1}' "$out.sha256")"
  got="$(shasum -a 256 "$out" | awk '{print $1}')"
  [[ "$want" == "$got" ]] || { echo "CHECKSUM MISMATCH: want=$want got=$got"; exit 1; }
  echo "ok: $got"
}

fetch "$BASE/$LISTENS_DIR/$LISTENS_FILE" "$DATA_DIR/$LISTENS_FILE"
fetch "$BASE/$CANON_DIR/$CANON_FILE" "$DATA_DIR/$CANON_FILE"
echo "done. artifacts in $DATA_DIR"
