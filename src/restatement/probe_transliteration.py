"""Probe the transliteration rule on the real residual BEFORE implementing anything.

READ-ONLY. Nothing is written to BigQuery, no normalization version is bumped, no candidate table is
touched. The probe answers one question with measurements: does transliterating these scripts produce
usable blocking keys, or does it produce ambiguity?

The alternatives and the rule for choosing between them were frozen in
config/restatement_trigger.yml before this ran. Nothing here may consult a reference label, a
calibration partition, a validation partition, or how much money the change would move.

HOW IT WORKS. Both sides get the SAME treatment, which is the only way a blocking join can work:

    listen side     cohort raw strings -> transliterate -> existing normalizer -> lookup key
    canonical side  MusicBrainz rows with non-Latin titles -> same two steps -> lookup key

Then the keys are joined in memory and the candidate distribution is measured. Doing the join
locally rather than in BigQuery keeps the probe free of cost and free of side effects: nothing is
staged, so nothing has to be cleaned up.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from normalization import load_rules, normalize
from normalization.transliterate import (
    SCRIPT_HANGUL,
    SCRIPT_KANA,
    script_inventory,
    transliterate,
    would_transliterate,
)

PROJECT = "ss-de-944054e7"
MAX_BYTES = 100 * 1024**3
ON_DEMAND_USD_PER_TIB = 6.25

COHORT_REASONS = ("NO_LOOKUP_KEY_PARTIAL", "NO_LOOKUP_KEY_EMPTY")
HANGUL_RE = r"[\x{AC00}-\x{D7A3}\x{1100}-\x{11FF}\x{3130}-\x{318F}]"
KANA_RE = r"[\x{3040}-\x{30FF}]"

ALTERNATIVES = {
    "A_conservative": (SCRIPT_HANGUL,),
    "B_broader": (SCRIPT_HANGUL, SCRIPT_KANA),
}
#: From config/restatement_trigger.yml, restated here so the code applies the frozen rule.
MIN_UNIQUE_CANDIDATE_RATE = 0.90
MAX_CANDIDATES_PER_KEY = 100


def cohort_pairs(client, stats):
    """Distinct raw (artist, recording) pairs in the residual, with their listen counts."""
    from google.cloud import bigquery

    sql = f"""
        SELECT n.artist_name, n.recording_name, m.failure_reason,
               COUNT(*) AS listens
        FROM `{PROJECT}.splitsheet_silver.silver_listen_matches` m
        JOIN `{PROJECT}.splitsheet_silver.silver_listens_normalized` n USING (listen_hash)
        WHERE m.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND m.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
          AND n.listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
          AND n.listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
          AND m.failure_reason IN {COHORT_REASONS}
        GROUP BY 1, 2, 3
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
    rows = [
        (r["artist_name"], r["recording_name"], r["failure_reason"], int(r["listens"]))
        for r in job.result()
    ]
    stats.append(
        {
            "step": "cohort pairs",
            "job_id": job.job_id,
            "bytes_billed": job.total_bytes_billed,
            "slot_ms": job.slot_millis,
            "rows": len(rows),
        }
    )
    print(
        f"  cohort: {len(rows):,} distinct pairs, "
        f"{sum(r[3] for r in rows):,} listens  billed={job.total_bytes_billed or 0:,}",
        flush=True,
    )
    return rows


def canonical_rows(client, scripts: tuple[str, ...], stats):
    """Canonical recordings whose title contains a character in the enabled scripts.

    Only the enabled scripts are pulled: the conservative alternative needs 65k rows, the broader one
    needs about a million, and pulling the larger set for both would be work the conservative
    alternative never uses.
    """
    from google.cloud import bigquery

    patterns = []
    if SCRIPT_HANGUL in scripts:
        patterns.append(f"REGEXP_CONTAINS(recording_name, r'{HANGUL_RE}')")
    if SCRIPT_KANA in scripts:
        patterns.append(f"REGEXP_CONTAINS(recording_name, r'{KANA_RE}')")
    sql = f"""
        SELECT recording_mbid, artist_credit_name, recording_name
        FROM `{PROJECT}.splitsheet_bronze.bronze_canonical_recordings`
        WHERE snapshot_date = DATE '2026-07-17' AND ({' OR '.join(patterns)})
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
    result = job.result()
    stats.append(
        {
            "step": f"canonical rows for {'+'.join(scripts)}",
            "job_id": job.job_id,
            "bytes_billed": job.total_bytes_billed,
            "slot_ms": job.slot_millis,
            "rows": result.total_rows,
        }
    )
    print(
        f"  canonical for {'+'.join(scripts)}: {result.total_rows:,} rows  "
        f"billed={job.total_bytes_billed or 0:,}",
        flush=True,
    )
    return result


def evaluate(name: str, scripts: tuple[str, ...], cohort, client, rules, stats) -> dict:
    """Measure one alternative end to end. Returns the evidence, not a verdict."""
    print(f"\n  == {name}: scripts {scripts}", flush=True)

    # --- canonical side: key -> candidate recordings ---------------------------------------
    index: dict[str, list[str]] = defaultdict(list)
    canonical_keyed = canonical_covered = canonical_seen = 0
    for row in canonical_rows(client, scripts, stats):
        canonical_seen += 1
        artist_t, artist_ok = transliterate(row["artist_credit_name"] or "", scripts)
        title_t, title_ok = transliterate(row["recording_name"] or "", scripts)
        if not (artist_ok and title_ok):
            continue
        canonical_covered += 1
        n = normalize(artist_t, title_t, rules=rules)
        if n.lookup_exact:
            canonical_keyed += 1
            bucket = index[n.lookup_exact]
            # Cap the stored candidates: the count is what matters, and holding every MBID for a
            # pathological key is how a probe runs the machine out of memory.
            if len(bucket) < MAX_CANDIDATES_PER_KEY + 1:
                bucket.append(row["recording_mbid"])
    print(
        f"     canonical: {canonical_seen:,} scanned, {canonical_covered:,} fully covered, "
        f"{canonical_keyed:,} produced a key, {len(index):,} distinct keys",
        flush=True,
    )

    # --- listen side ------------------------------------------------------------------------
    scripts_found: Counter = Counter()
    affected_pairs = affected_listens = 0
    keyed_pairs = keyed_listens = 0
    unique_pairs = unique_listens = 0
    multi_pairs = multi_listens = 0
    zero_pairs = zero_listens = 0
    candidate_counts: Counter = Counter()
    over_cap_keys: dict[str, int] = {}
    examples: dict[str, list[dict]] = {"korean": [], "japanese": [], "other": []}
    by_reason: Counter = Counter()

    for artist, recording, reason, listens in cohort:
        inventory = script_inventory(f"{artist} {recording}")
        for script, count in inventory.items():
            if count and script not in ("latin_or_neutral",):
                scripts_found[script] += listens
        if not (would_transliterate(artist, scripts) or would_transliterate(recording, scripts)):
            continue
        affected_pairs += 1
        affected_listens += listens

        artist_t, artist_ok = transliterate(artist, scripts)
        title_t, title_ok = transliterate(recording, scripts)
        if not (artist_ok and title_ok):
            # Not fully covered: usually kanji in the title. No key, by design.
            zero_pairs += 1
            zero_listens += listens
            continue
        n = normalize(artist_t, title_t, rules=rules)
        if not n.lookup_exact:
            zero_pairs += 1
            zero_listens += listens
            continue

        keyed_pairs += 1
        keyed_listens += listens
        by_reason[reason] += listens
        candidates = index.get(n.lookup_exact, [])
        count = len(candidates)
        candidate_counts[min(count, MAX_CANDIDATES_PER_KEY + 1)] += listens
        if count > MAX_CANDIDATES_PER_KEY:
            over_cap_keys[n.lookup_exact] = count
        if count == 1:
            unique_pairs += 1
            unique_listens += listens
        elif count > 1:
            multi_pairs += 1
            multi_listens += listens
        else:
            zero_pairs += 1
            zero_listens += listens

        if len(examples["korean"]) < 6 and inventory["hangul"]:
            examples["korean"].append(
                {
                    "artist": artist,
                    "recording": recording,
                    "listens": listens,
                    "transliterated": f"{artist_t} / {title_t}",
                    "lookup_key": n.lookup_exact,
                    "candidates": count,
                    "candidate_sample": candidates[:3],
                }
            )
        elif len(examples["japanese"]) < 6 and (inventory["kana"] or inventory["han"]):
            examples["japanese"].append(
                {
                    "artist": artist,
                    "recording": recording,
                    "listens": listens,
                    "transliterated": f"{artist_t} / {title_t}",
                    "lookup_key": n.lookup_exact,
                    "candidates": count,
                    "candidate_sample": candidates[:3],
                }
            )

    matchable = unique_listens + multi_listens
    unique_rate = (unique_listens / matchable) if matchable else 0.0
    result = {
        "alternative": name,
        "scripts": list(scripts),
        "canonical_scanned": canonical_seen,
        "canonical_fully_covered": canonical_covered,
        "canonical_keyed": canonical_keyed,
        "canonical_distinct_keys": len(index),
        "affected_pairs": affected_pairs,
        "affected_listens": affected_listens,
        "pairs_that_produced_a_key": keyed_pairs,
        "listens_that_produced_a_key": keyed_listens,
        "listens_with_a_unique_candidate": unique_listens,
        "listens_with_multiple_candidates": multi_listens,
        "listens_with_no_candidate": zero_listens,
        "unique_candidate_rate": round(unique_rate, 6),
        "keys_over_candidate_cap": len(over_cap_keys),
        "worst_key_candidates": (
            max(over_cap_keys.values())
            if over_cap_keys
            else (max(candidate_counts) if candidate_counts else 0)
        ),
        "newly_keyed_listens_by_prior_failure_reason": dict(by_reason),
        "candidate_count_distribution_by_listens": {
            str(k): v for k, v in sorted(candidate_counts.items())
        },
        "examples": examples,
        "constraints": {
            "unique_candidate_rate_ok": unique_rate >= MIN_UNIQUE_CANDIDATE_RATE,
            "no_key_over_cap_ok": not over_cap_keys,
        },
    }
    result["satisfies_frozen_constraints"] = all(result["constraints"].values())
    print(
        f"     listens affected {affected_listens:,}; keyed {keyed_listens:,}; "
        f"unique {unique_listens:,}; multi {multi_listens:,}; none {zero_listens:,}"
    )
    print(
        f"     unique-candidate rate {unique_rate:.4f} "
        f"(constraint >= {MIN_UNIQUE_CANDIDATE_RATE})  "
        f"keys over {MAX_CANDIDATES_PER_KEY} candidates: {len(over_cap_keys)}"
    )
    print(f"     satisfies frozen constraints: {result['satisfies_frozen_constraints']}")
    index.clear()
    return result, scripts_found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from google.cloud import bigquery

    rules = load_rules()
    client = bigquery.Client(project=PROJECT)
    stats: list = []
    t0 = time.time()
    print(
        f"probe under normalization {rules.version} (unchanged; this probe writes nothing)",
        flush=True,
    )

    cohort = cohort_pairs(client, stats)
    results = []
    inventory_by_listens: Counter = Counter()
    for name, scripts in ALTERNATIVES.items():
        result, found = evaluate(name, scripts, cohort, client, rules, stats)
        results.append(result)
        inventory_by_listens.update(found)

    # --- apply the FROZEN selection rule ---------------------------------------------------
    eligible = [r for r in results if r["satisfies_frozen_constraints"]]
    if eligible:
        chosen = max(eligible, key=lambda r: r["listens_that_produced_a_key"])
        decision = (
            "frozen rule: among alternatives satisfying both constraints, the one keying "
            "the most listens"
        )
    else:
        chosen = None
        decision = (
            "NO alternative satisfied the frozen constraints; the frozen rule says "
            "implement nothing and report a negative result"
        )

    report = {
        "artifact": "transliteration_probe",
        "read_only": True,
        "normalization_version_during_probe": rules.version,
        "trigger_id": "RT-1",
        "forbidden_inputs_used": "none: no reference label, no calibration, no validation, no money",
        "selection_rule": {
            "objective": "maximise listens that gain a usable combined lookup key",
            "min_unique_candidate_rate": MIN_UNIQUE_CANDIDATE_RATE,
            "max_candidates_per_key": MAX_CANDIDATES_PER_KEY,
            "frozen_before_probe": True,
        },
        "cohort": {
            "failure_reasons": list(COHORT_REASONS),
            "distinct_pairs": len(cohort),
            "listens": sum(r[3] for r in cohort),
            "script_inventory_by_listens": dict(inventory_by_listens.most_common()),
        },
        "alternatives": results,
        "chosen_alternative": chosen["alternative"] if chosen else None,
        "decision_basis": decision,
        "cost": {
            "bytes_billed": sum(s.get("bytes_billed") or 0 for s in stats),
            "list_price_equivalent_usd": round(
                sum(s.get("bytes_billed") or 0 for s in stats) / 1024**4 * ON_DEMAND_USD_PER_TIB, 4
            ),
            "caveat": "list-price equivalent; actual monetary cost UNKNOWN without billing evidence",
        },
        "wall_seconds": round(time.time() - t0, 1),
        "jobs": stats,
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, default=str))

    print("\n  SCRIPT INVENTORY over the cohort (listens):")
    for script, listens in inventory_by_listens.most_common():
        print(f"    {script:<20} {listens:>12,}")
    print(f"\n  DECISION: {chosen['alternative'] if chosen else 'IMPLEMENT NOTHING'}")
    print(f"    {decision}")
    for r in results:
        print(
            f"    {r['alternative']:<16} keyed={r['listens_that_produced_a_key']:>10,}  "
            f"unique_rate={r['unique_candidate_rate']:.4f}  "
            f"over_cap_keys={r['keys_over_candidate_cap']}  "
            f"ok={r['satisfies_frozen_constraints']}"
        )


if __name__ == "__main__":
    main()
