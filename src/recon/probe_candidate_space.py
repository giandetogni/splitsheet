"""Measure the real candidate space between listens and canonical recordings.

Exists to answer three Phase 0 questions with numbers instead of intuition:
  1. how many listens find zero / one / many canonical candidates,
  2. how much ambiguity aggressive suffix-stripping buys or costs,
  3. whether the join is large enough to need Spark at all.

This is a measurement probe, NOT the production normalizer. Its only job is to size
the problem so the real matcher can be designed against evidence.

Two modes, because the two sides arrive as separate streams:
  keys  < listens.jsonl   --out keys.json
  join  < canonical.csv   --keys keys.json --out report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict

from profile_listens import PAREN_RE, SUFFIX_RES, WS_RE

FEAT_RE = re.compile(r"\b(feat|ft|featuring|with)\b.*$", re.IGNORECASE)
ASCII_DEL = str.maketrans("", "", "".join(c for c in map(chr, range(128)) if not c.isalnum()))


def fold(text: str) -> str:
    """Reduce a string to ASCII alphanumerics, mirroring MusicBrainz's combined_lookup.

    Note the known limitation: MusicBrainz romanises non-Latin scripts, which requires
    a transliteration table we do not have. Here such characters are dropped, so a
    CJK title folds to the empty string. Those cases are counted, not hidden.
    """
    if not text.isascii():
        text = "".join(
            c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
        )
    text = text.lower()
    if text.isascii():
        return text.translate(ASCII_DEL)
    return "".join(c for c in text if c.isascii() and c.isalnum())


def strip_aggressive(text: str) -> str:
    """The aggressive end of normalization: drop bracketed segments and version markers."""
    out = PAREN_RE.sub(" ", text.lower())
    for _, rx in SUFFIX_RES:
        out = rx.sub(" ", out)
    return WS_RE.sub(" ", out).strip(" -–—")


def keys_for(artist: str, track: str) -> tuple[str, str]:
    """Return (exact_key, aggressive_key); empty string means 'no usable key'."""
    a, t = fold(artist), fold(track)
    exact = a + t if a and t else ""
    a2 = fold(FEAT_RE.sub(" ", artist))
    t2 = fold(strip_aggressive(track))
    aggr = a2 + t2 if a2 and t2 else ""
    return exact, aggr


def mode_keys(args: argparse.Namespace) -> None:
    exact: Counter = Counter()
    aggr: Counter = Counter()
    c: Counter = Counter()
    for line in sys.stdin:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            c["parse_errors"] += 1
            continue
        ts = rec.get("timestamp")
        if not isinstance(ts, int):
            continue
        if args.only_month and time.strftime("%Y-%m", time.gmtime(ts)) != args.only_month:
            continue
        c["listens_in_scope"] += 1
        tm = rec.get("track_metadata") or {}
        artist, track = tm.get("artist_name"), tm.get("track_name")
        if not isinstance(artist, str) or not isinstance(track, str):
            c["listens_missing_strings"] += 1
            continue
        ek, ak = keys_for(artist, track)
        if not ek:
            c["listens_no_usable_key"] += 1
        else:
            exact[ek] += 1
        if ak:
            aggr[ak] += 1
    out = {
        "counts": dict(c),
        "distinct_exact_keys": len(exact),
        "distinct_aggressive_keys": len(aggr),
        "exact": exact,
        "aggressive": aggr,
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh)
    print(json.dumps({k: out[k] for k in
                      ("counts", "distinct_exact_keys", "distinct_aggressive_keys")}, indent=2))


def mode_join(args: argparse.Namespace) -> None:
    with open(args.keys) as fh:
        side = json.load(fh)
    exact_w: dict[str, int] = side["exact"]
    aggr_w: dict[str, int] = side["aggressive"]
    exact_hits: dict[str, set[str]] = defaultdict(set)
    aggr_hits: dict[str, set[str]] = defaultdict(set)
    c: Counter = Counter()

    csv.field_size_limit(10**9)
    reader = csv.reader(sys.stdin)
    hdr = next(reader)
    col = {n: i for i, n in enumerate(hdr)}
    ia, it, ir, icl = (col["artist_credit_name"], col["recording_name"],
                       col["recording_mbid"], col["combined_lookup"])

    for row in reader:
        if len(row) != len(hdr):
            continue
        c["canonical_rows"] += 1
        ek, ak = keys_for(row[ia], row[it])
        if not ek:
            c["canonical_no_usable_key"] += 1
        elif ek == row[icl]:
            c["our_key_equals_combined_lookup"] += 1
        if ek and ek in exact_w:
            exact_hits[ek].add(row[ir])
        if ak and ak in aggr_w:
            aggr_hits[ak].add(row[ir])

    def summarize(weights: dict[str, int], hits: dict[str, set[str]], label: str) -> dict:
        total_listens = sum(weights.values())
        zero = one = many = 0
        zero_l = one_l = many_l = 0
        cand_per_listen: list[tuple[int, int]] = []
        for key, w in weights.items():
            n = len(hits.get(key, ()))
            cand_per_listen.append((n, w))
            if n == 0:
                zero += 1; zero_l += w
            elif n == 1:
                one += 1; one_l += w
            else:
                many += 1; many_l += w
        cand_per_listen.sort()
        # percentiles weighted by listens, not by distinct key
        acc, p50, p99, pmax = 0, 0, 0, (cand_per_listen[-1][0] if cand_per_listen else 0)
        for n, w in cand_per_listen:
            acc += w
            if not p50 and acc >= total_listens * 0.50:
                p50 = n
            if not p99 and acc >= total_listens * 0.99:
                p99 = n
        weighted_sum = sum(n * w for n, w in cand_per_listen)
        return {
            "label": label,
            "distinct_keys": len(weights),
            "listens_covered": total_listens,
            "keys_zero_candidates": zero,
            "keys_one_candidate": one,
            "keys_many_candidates": many,
            "listens_zero_candidates_pct": round(100 * zero_l / max(total_listens, 1), 4),
            "listens_one_candidate_pct": round(100 * one_l / max(total_listens, 1), 4),
            "listens_many_candidates_pct": round(100 * many_l / max(total_listens, 1), 4),
            "mean_candidates_per_listen": round(weighted_sum / max(total_listens, 1), 4),
            "p50_candidates_per_listen": p50,
            "p99_candidates_per_listen": p99,
            "max_candidates_for_any_key": pmax,
            "total_candidate_pairs": weighted_sum,
        }

    report = {
        "listen_side": side["counts"],
        "canonical_counts": dict(c),
        "our_key_equals_combined_lookup_pct": round(
            100 * c["our_key_equals_combined_lookup"] / max(c["canonical_rows"], 1), 4),
        "exact": summarize(exact_w, exact_hits, "exact normalization"),
        "aggressive": summarize(aggr_w, aggr_hits, "aggressive suffix-stripped"),
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)
    json.dump(report, sys.stdout, indent=2)
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["keys", "join"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--keys")
    ap.add_argument("--only-month", default="")
    args = ap.parse_args()
    (mode_keys if args.mode == "keys" else mode_join)(args)


if __name__ == "__main__":
    main()
