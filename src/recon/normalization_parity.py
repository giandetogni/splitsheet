"""Re-run the Phase 0A candidate-space experiment using ONLY src/normalization.

Phase 0A's numbers (73.64% single candidate at exact, 26.02% none, 51.93% under blanket
aggressive normalization) came from a throwaway recon probe. Those numbers are quoted in
the design, the config and the tests, so they have to be reproducible from the production
library or they are just folklore.

Differences from the old probe are expected and are the point of the exercise. Three are
known in advance: years are now removed only inside recognised reissue structures, version
markers are only stripped when something precedes them, and no key is emitted from half a
pair. This script quantifies the effect rather than assuming it is small.

Runs entirely on local files. No GCP call, no credentials.

  keys < listens.jsonl   --out keys.json
  join < canonical.csv   --keys keys.json --out report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from normalization import load_rules, normalize
from recon.probe_candidate_space import keys_for as legacy_keys_for

RULES = load_rules()

_SCRIPT_RANGES = [
    ("Latin", "LATIN"), ("Cyrillic", "CYRILLIC"), ("Greek", "GREEK"),
    ("Arabic", "ARABIC"), ("Hebrew", "HEBREW"), ("Thai", "THAI"),
]
_CJK_PREFIXES = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL")


def script_group(text: str) -> str:
    """Dominant script of a string, by counting the letters that carry a script name."""
    counts: Counter = Counter()
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if name.startswith(_CJK_PREFIXES):
            counts["CJK"] += 1
            continue
        for label, prefix in _SCRIPT_RANGES:
            if name.startswith(prefix):
                counts[label] += 1
                break
        else:
            counts["Other"] += 1
    if not counts:
        return "NoLetters"
    return counts.most_common(1)[0][0]


def mode_keys(args: argparse.Namespace) -> None:
    """Normalize the listen side once per distinct (artist, recording) pair."""
    pairs: dict[tuple[str, str], dict] = {}
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
        c["listens_evaluated"] += 1

        tm = rec.get("track_metadata") or {}
        artist, recording = tm.get("artist_name") or "", tm.get("track_name") or ""
        key = (artist, recording)
        row = pairs.get(key)
        if row is None:
            n = normalize(artist, recording)
            legacy_exact, legacy_fb = legacy_keys_for(artist, recording)
            row = {
                "n": 0,
                "exact": n.lookup_exact,
                "fallback": n.lookup_fallback,
                "norm_status": n.normalization_status.value,
                "exact_status": n.exact_key_status.value,
                "fallback_status": n.fallback_key_status.value,
                "script": script_group(recording),
                "legacy_exact_same": n.lookup_exact == legacy_exact,
                "legacy_fallback_same": n.lookup_fallback == legacy_fb,
            }
            pairs[key] = row
        row["n"] += 1

    out = {"counts": dict(c), "distinct_pairs": len(pairs),
           "pairs": [{"a": a, "r": r, **v} for (a, r), v in pairs.items()]}
    with open(args.out, "w") as fh:
        json.dump(out, fh)
    print(json.dumps({"listens_evaluated": c["listens_evaluated"],
                      "distinct_pairs": len(pairs)}, indent=1))


def mode_join(args: argparse.Namespace) -> None:
    """Count distinct canonical recordings per key, using the same library on both sides."""
    with open(args.keys) as fh:
        side = json.load(fh)
    rows = side["pairs"]

    exact_hits: dict[str, set] = defaultdict(set)
    fallback_hits: dict[str, set] = defaultdict(set)
    wanted_exact = {r["exact"] for r in rows if r["exact"]}
    wanted_fallback = {r["fallback"] for r in rows if r["fallback"]}

    csv.field_size_limit(10**9)
    reader = csv.reader(sys.stdin)
    hdr = next(reader)
    col = {n: i for i, n in enumerate(hdr)}
    ia, it, ir = col["artist_credit_name"], col["recording_name"], col["recording_mbid"]
    canonical_rows = 0

    for row in reader:
        if len(row) != len(hdr):
            continue
        canonical_rows += 1
        n = normalize(row[ia], row[it])
        if n.lookup_exact and n.lookup_exact in wanted_exact:
            exact_hits[n.lookup_exact].add(row[ir])
        if n.lookup_fallback and n.lookup_fallback in wanted_fallback:
            fallback_hits[n.lookup_fallback].add(row[ir])
        if canonical_rows % 5_000_000 == 0:
            print(f"  canonical rows scanned: {canonical_rows:,}", flush=True)

    total = sum(r["n"] for r in rows)
    m: Counter = Counter()
    by_script: dict[str, Counter] = defaultdict(Counter)
    staged_pairs = 0

    for r in rows:
        w, script = r["n"], r["script"]
        by_script[script]["listens"] += w
        m["listens"] += w
        m[f"norm_{r['norm_status']}"] += w
        m[f"exact_key_{r['exact_status']}"] += w
        m[f"fallback_key_{r['fallback_status']}"] += w
        if r["legacy_exact_same"]:
            m["legacy_exact_identical"] += w
        if r["legacy_fallback_same"]:
            m["legacy_fallback_identical"] += w

        n_exact = len(exact_hits.get(r["exact"], ())) if r["exact"] else 0
        if n_exact == 0:
            m["exact_zero"] += w
            by_script[script]["exact_zero"] += w
            # Staged design: the fallback runs ONLY here.
            if r["fallback"]:
                m["fallback_executed"] += w
                staged_pairs += 1
                n_fb = len(fallback_hits.get(r["fallback"], ()))
                if n_fb == 1:
                    m["staged_unique_after_fallback"] += w
                elif n_fb > 1:
                    m["staged_multiple_after_fallback"] += w
                else:
                    m["staged_still_zero"] += w
            else:
                m["fallback_unavailable"] += w
        elif n_exact == 1:
            m["exact_unique"] += w
            by_script[script]["exact_unique"] += w
        else:
            m["exact_multiple"] += w
            by_script[script]["exact_multiple"] += w

        # Blanket aggressive, for comparability with the old 51.93% figure only.
        if r["fallback"]:
            n_fb_all = len(fallback_hits.get(r["fallback"], ()))
            if n_fb_all == 1:
                m["blanket_fallback_unique"] += w
            elif n_fb_all > 1:
                m["blanket_fallback_multiple"] += w
            else:
                m["blanket_fallback_zero"] += w

    def pct(k: str) -> float:
        return round(100 * m[k] / max(total, 1), 4)

    report = {
        "listens_evaluated": total,
        "distinct_pairs": len(rows),
        "canonical_rows_scanned": canonical_rows,
        "normalization_version": RULES.version,
        "coverage": {
            "exact_key_available_pct": pct("exact_key_AVAILABLE"),
            "exact_key_partial_pct": pct("exact_key_PARTIAL"),
            "exact_key_empty_pct": pct("exact_key_EMPTY"),
            "fallback_key_available_pct": pct("fallback_key_AVAILABLE"),
            "fallback_key_partial_pct": pct("fallback_key_PARTIAL"),
            "fallback_key_empty_pct": pct("fallback_key_EMPTY"),
        },
        "content_status_pct": {
            k.replace("norm_", ""): pct(k) for k in m if k.startswith("norm_")
        },
        "agreement_with_legacy_probe": {
            "exact_key_identical_pct": pct("legacy_exact_identical"),
            "fallback_key_identical_pct": pct("legacy_fallback_identical"),
        },
        "exact_stage": {
            "unique_candidate_pct": pct("exact_unique"),
            "zero_candidate_pct": pct("exact_zero"),
            "multiple_candidates_pct": pct("exact_multiple"),
        },
        "staged_fallback": {
            "fallback_executed_pct": pct("fallback_executed"),
            "fallback_unavailable_pct": pct("fallback_unavailable"),
            "unique_after_fallback_pct": pct("staged_unique_after_fallback"),
            "multiple_after_fallback_pct": pct("staged_multiple_after_fallback"),
            "still_zero_pct": pct("staged_still_zero"),
            "pairs_entering_fallback": staged_pairs,
        },
        "blanket_aggressive_for_comparison": {
            "unique_candidate_pct": pct("blanket_fallback_unique"),
            "multiple_candidates_pct": pct("blanket_fallback_multiple"),
            "zero_candidate_pct": pct("blanket_fallback_zero"),
        },
        "phase0a_baseline": {
            "exact_unique_candidate_pct": 73.6363,
            "exact_zero_candidate_pct": 26.0246,
            "blanket_aggressive_unique_pct": 51.9275,
        },
        "by_script_group": {
            s: {
                "listens": v["listens"],
                "share_of_listens_pct": round(100 * v["listens"] / max(total, 1), 4),
                "exact_unique_pct": round(100 * v["exact_unique"] / max(v["listens"], 1), 4),
                "exact_zero_pct": round(100 * v["exact_zero"] / max(v["listens"], 1), 4),
            }
            for s, v in sorted(by_script.items(), key=lambda kv: -kv[1]["listens"])
        },
        "raw_counts": dict(m),
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(json.dumps({k: report[k] for k in
                      ("listens_evaluated", "coverage", "agreement_with_legacy_probe",
                       "exact_stage", "staged_fallback",
                       "blanket_aggressive_for_comparison")}, indent=1))


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
