"""Profile a ListenBrainz .listens JSONL stream read from stdin.

Exists to answer Phase 0 questions with measured numbers instead of assumptions:
which identifier origins are actually populated, how much of the matching problem
is already solved upstream, and how much ambiguity aggressive suffix-stripping
would create. Emits aggregates only -- never a user_name -- so its output is safe
to commit to a public repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Any

# Version suffixes we expect to matter. Kept here (not config/) because this is a
# measurement probe: the production list lives in config/normalization_rules.yml
# and is derived from what this probe finds.
SUFFIX_PATTERNS = [
    ("remaster", r"\bremaster(ed)?\b"),
    ("live", r"\blive\b"),
    ("deluxe", r"\bdeluxe\b"),
    ("radio_edit", r"\bradio edit\b"),
    ("mono", r"\bmono\b"),
    ("stereo", r"\bstereo\b"),
    ("bonus_track", r"\bbonus track\b"),
    ("anniversary", r"\banniversary\b"),
    ("explicit", r"\bexplicit\b"),
    ("album_version", r"\balbum version\b"),
    ("single_version", r"\bsingle version\b"),
    ("year4", r"\b(19|20)\d{2}\b"),
    ("feat", r"\b(feat|ft)\.?\b"),
    ("version", r"\bversion\b"),
    ("mix", r"\b(re)?mix\b"),
]
SUFFIX_RES = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in SUFFIX_PATTERNS]
PAREN_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$", re.IGNORECASE)
WS_RE = re.compile(r"\s+")

# Plausible listen window. Outside this range a timestamp is treated as invalid
# rather than silently creating a partition years away from the data.
TS_MIN = 1_000_000_000  # 2001-09-09
TS_MAX = 1_900_000_000  # 2030-03-17


def strip_version(text: str) -> str:
    """Approximate the aggressive end of normalization, to size the ambiguity cost."""
    out = PAREN_RE.sub(" ", text.lower())
    for _, rx in SUFFIX_RES:
        out = rx.sub(" ", out)
        out = out.replace(" - ", " ")
    return WS_RE.sub(" ", out).strip(" -–—")


def walk(obj: Any, prefix: str, presence: Counter, types: dict[str, Counter]) -> None:
    """Record the real, observed field inventory including nested structures."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            presence[path] += 1
            types[path][type(val).__name__] += 1
            if isinstance(val, (dict, list)):
                walk(val, path, presence, types)
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        walk(obj[0], f"{prefix}[]", presence, types)


def h64(*parts: object) -> int:
    """Cheap 8-byte key so duplicate detection over millions of rows stays in RAM."""
    raw = "\x1f".join("" if p is None else str(p) for p in parts).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N lines (0=all)")
    ap.add_argument("--out", required=True, help="path to write JSON report")
    ap.add_argument("--top", type=int, default=50)
    # A submission-day dump is dominated by bulk historical backfills, so
    # unfiltered coverage rates are not representative of any listening period.
    ap.add_argument("--only-month", default="", help="keep only listened_at in YYYY-MM")
    args = ap.parse_args()

    presence: Counter = Counter()
    types: dict[str, Counter] = defaultdict(Counter)
    addl_keys: Counter = Counter()
    c: Counter = Counter()
    suffix_hits: Counter = Counter()
    top_tracks: Counter = Counter()
    mapping_extra: Counter = Counter()
    clients: Counter = Counter()
    listened_month: Counter = Counter()
    users: set[int] = set()
    artists: set[str] = set()
    pairs_raw: set[int] = set()
    pairs_stripped: set[int] = set()
    dup_keys: set[int] = set()
    ts_min, ts_max = None, None

    for i, line in enumerate(sys.stdin):
        if args.limit and i >= args.limit:
            break
        c["total_lines"] += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            c["parse_errors"] += 1
            continue
        if args.only_month:
            _ts = rec.get("timestamp")
            if not isinstance(_ts, int) or time.strftime("%Y-%m", time.gmtime(_ts)) != args.only_month:
                c["filtered_out_other_month"] += 1
                continue
            c["kept_in_month"] += 1
        walk(rec, "", presence, types)

        tm = rec.get("track_metadata") or {}
        ai = tm.get("additional_info") or {}
        mm = tm.get("mbid_mapping") or {}
        addl_keys.update(ai.keys())
        mapping_extra.update(mm.keys())
        if isinstance(ai.get("submission_client"), str):
            clients[ai["submission_client"]] += 1

        # --- identifier origins, measured separately (decision B.1) ---
        client_mbid = ai.get("recording_mbid")
        map_mbid = mm.get("recording_mbid")
        if isinstance(client_mbid, str):
            c["client_recording_mbid"] += 1
            if not UUID_RE.match(client_mbid):
                c["client_recording_mbid_malformed"] += 1
        if isinstance(map_mbid, str):
            c["mapping_recording_mbid"] += 1
        if isinstance(client_mbid, str) and isinstance(map_mbid, str):
            c["both_mbid_origins"] += 1
            c["mbid_origins_agree"] += int(client_mbid.lower() == map_mbid.lower())
        if not isinstance(client_mbid, str) and not isinstance(map_mbid, str):
            c["no_mbid_any_origin"] += 1
        if isinstance(ai.get("artist_mbids"), list) and ai["artist_mbids"]:
            c["client_artist_mbids"] += 1
        if isinstance(ai.get("release_mbid"), str):
            c["client_release_mbid"] += 1

        # --- ISRC: check every plausible spelling ---
        isrc = ai.get("isrc") or ai.get("ISRC") or ai.get("isrc_list")
        if isinstance(isrc, list):
            isrc = isrc[0] if isrc else None
        if isinstance(isrc, str) and isrc.strip():
            c["isrc_present"] += 1
            c["isrc_valid_format"] += int(bool(ISRC_RE.match(isrc.replace("-", ""))))

        # --- duration ---
        dur = ai.get("duration_ms")
        if dur is None and ai.get("duration") is not None:
            dur = ai["duration"] * 1000 if isinstance(ai["duration"], (int, float)) else None
            c["duration_from_seconds_field"] += 1
        if isinstance(dur, (int, float)) and dur > 0:
            c["duration_present"] += 1

        # --- timestamp validity ---
        ts = rec.get("timestamp")
        if rec.get("listened_at") is not None:
            c["has_listened_at_field"] += 1
        if isinstance(ts, int):
            ts_min = ts if ts_min is None else min(ts_min, ts)
            ts_max = ts if ts_max is None else max(ts_max, ts)
            if ts < TS_MIN:
                c["ts_too_old"] += 1
            elif ts > TS_MAX:
                c["ts_too_new"] += 1
            else:
                c["ts_plausible"] += 1
                # Temporal distribution: a submission-day dump spans many listen
                # periods, which is what creates late-arrival restatement pressure.
                listened_month[time.strftime("%Y-%m", time.gmtime(ts))] += 1
        else:
            c["ts_missing_or_nonint"] += 1

        # --- strings, cardinality, ambiguity precursor ---
        artist = tm.get("artist_name")
        track = tm.get("track_name")
        if not isinstance(artist, str) or not artist.strip():
            c["null_or_blank_artist"] += 1
        if not isinstance(track, str) or not track.strip():
            c["null_or_blank_track"] += 1
        if isinstance(artist, str) and isinstance(track, str):
            artists.add(artist)
            top_tracks[track] += 1
            pairs_raw.add(h64(artist.lower(), track.lower()))
            pairs_stripped.add(h64(artist.lower(), strip_version(track)))
            matched = [n for n, rx in SUFFIX_RES if rx.search(track)]
            if matched:
                c["track_has_version_marker"] += 1
                suffix_hits.update(matched)
            if PAREN_RE.search(track):
                c["track_has_bracketed_segment"] += 1

        if isinstance(rec.get("user_id"), int):
            users.add(rec["user_id"])
        k = h64(rec.get("user_id"), ts, rec.get("recording_msid"))
        if k in dup_keys:
            c["dup_on_user_ts_msid"] += 1
        else:
            dup_keys.add(k)

    total = c["total_lines"]
    # Denominator must be the rows actually profiled, not every row read: with
    # --only-month the filtered-out rows never reach any counter, and dividing by
    # total would silently understate every rate.
    parsed = c["kept_in_month"] if args.only_month else total - c["parse_errors"]
    report = {
        "counts": dict(c),
        "derived": {
            "parsed_rows": parsed,
            "distinct_users": len(users),
            "distinct_artist_strings": len(artists),
            "distinct_artist_track_raw": len(pairs_raw),
            "distinct_artist_track_suffix_stripped": len(pairs_stripped),
            "suffix_collapse_ratio": round(len(pairs_raw) / max(len(pairs_stripped), 1), 4),
            "ts_min": ts_min,
            "ts_max": ts_max,
        },
        "rates_pct_of_parsed": {
            k: round(100.0 * c[k] / max(parsed, 1), 4)
            for k in (
                "client_recording_mbid", "mapping_recording_mbid", "both_mbid_origins",
                "no_mbid_any_origin", "isrc_present", "duration_present",
                "null_or_blank_artist", "null_or_blank_track",
                "track_has_version_marker", "dup_on_user_ts_msid",
            )
        },
        "mbid_agreement_pct_of_both": round(
            100.0 * c["mbid_origins_agree"] / max(c["both_mbid_origins"], 1), 4
        ),
        "field_presence_pct": {
            k: round(100.0 * v / max(parsed, 1), 4) for k, v in presence.most_common()
        },
        "field_types": {k: dict(v) for k, v in types.items()},
        "additional_info_keys_pct": {
            k: round(100.0 * v / max(parsed, 1), 4) for k, v in addl_keys.most_common()
        },
        "mbid_mapping_keys_pct": {
            k: round(100.0 * v / max(parsed, 1), 4) for k, v in mapping_extra.most_common()
        },
        "listened_at_month_top20": dict(listened_month.most_common(20)),
        "listened_at_distinct_months": len(listened_month),
        "suffix_marker_hits": dict(suffix_hits.most_common()),
        "top_submission_clients": dict(clients.most_common(15)),
        "top_track_strings": [[t, n] for t, n in top_tracks.most_common(args.top)],
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    json.dump({k: report[k] for k in ("counts", "derived", "rates_pct_of_parsed")},
              sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
