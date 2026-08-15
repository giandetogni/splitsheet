# Restatement candidates

A restatement is not a bug fix. It changes a published number, so it needs a version bump, a
re-publication, and both figures side by side. This file is the register of changes that are
**known to be worth making and deliberately not made yet**, with the evidence captured at the
moment the decision was taken, so a future phase can quantify exactly what moved.

Nothing in this file may be acted on inside Phase 5. The Phase 4B result is frozen; see
`config/frozen_versions.yml`.

---

## RC-1 — Transliteration of Korean and Japanese titles

**Status:** registered 2026-08-04, deferred to the Phase 6 restatement scenario. Not
implemented. The matcher is untouched.

**What it would change:** the normalization rules, therefore `normalization_version`, therefore
every downstream run id. It is the largest single recoverable block of unmatched listens
identified so far.

### Evidence preserved at the freeze

Source artifacts (committed, not regenerated): `docs/phase0/top_unmatched.json`,
`docs/phase0/match_results_scored_run.json`. All figures below are from the published Phase 4B
result under the frozen versions.

| frozen input | value |
|---|---|
| `normalization_version` | `1.0.0+0bc0dd643e06` |
| `scoring_version` | `1.0.0+cb21f9704ff0` |
| `candidate_run_id` | `blk:c005e9a56b1ec542` |
| `match_run_id` | `match:101eef5c5b5c081e` |
| canonical snapshot | 2026-07-17 |

**Current failure reason: `NO_LOOKUP_KEY_PARTIAL`.** Baseline count to restate against:

| metric | value |
|---|---|
| listens with `failure_reason = NO_LOOKUP_KEY_PARTIAL` | **1,164,629** |
| share of the 38,199,641-listen corpus | **3.0488 %** |
| share of the reason held by its top 20 artist/recording combinations | **35.34 %** |
| `match_status` for all of them | `UNRESOLVED` |
| `candidate_count` for all of them | 0 |

**Why the key is partial, and why that is correct today.** The listen carries a Latin artist
string and a non-Latin title. `artist_lookup_exact` survives ASCII folding; the title leaves no
ASCII content; the combined key is therefore never emitted. Emitting half a key would collapse
every unromanisable title by that artist into one block — the rule exists for a measured reason
and is not being relaxed. Transliteration attacks the cause instead: give the title an ASCII
representation so the combined key can exist at all.

### The concentration that makes this the top candidate

| rank | listens | script | artist (submitted) | recording (submitted) | share of reason |
|---|---|---|---|---|---|
| 1 | **384,926** | LATIN artist / non-Latin title | `Agust D` | `해금` | **33.05 %** |
| 2 | 6,656 | LATIN artist / Latin title | `범규` | `Panic` | 0.57 % |
| 3 | 2,175 | non-Latin | `BTS` | `팔도강산` | 0.19 % |
| 4 | 2,157 | LATIN artist / non-Latin title | `TOMORROW X TOGETHER` | `다음의 다음` | 0.19 % |
| 5 | 1,740 | LATIN artist / non-Latin title | `YOASOBI` | `アイドル` | 0.15 % |
| 6 | 1,345 | non-Latin | `Ado` | `うっせぇわ` | 0.12 % |

**`Agust D` / `해금` alone is 384,926 listens — 33.05 % of the entire failure reason and
1.01 % of the whole corpus.** One recording. That single row is the reason this candidate ranks
above every scoring change currently available: no threshold, weight or margin in
`config/scoring_rules.yml` can reach a listen that produced zero candidates.

Aggregated examples only. No `user_id`, no `recording_msid`, no `listen_hash` — these are
submitted content strings and counts.

### What a restatement would have to show

1. a new `normalization_version` and a re-run of blocking, features and scoring under it;
2. the 1,164,629 baseline and the new `NO_LOOKUP_KEY_PARTIAL` count side by side;
3. what happened to the 384,926 `해금` listens specifically — matched, still partial, or moved
   to a different failure reason;
4. that transliteration did not degrade the frozen numbers elsewhere: `EXACT_MULTIPLE`
   disagreement of 0.1101 % on the validation partition is the bar, and it cannot be re-measured
   on that partition, so a new evaluation strategy is required first;
5. that no ambiguity was created that the current tie policy would silently accept — a
   transliterated key can collide two genuinely different recordings, which is exactly the
   failure mode `AMBIGUOUS_TIE` exists to catch.

Point 4 is the reason this is a Phase 6 restatement and not a Phase 5 improvement: the
validation partition is consumed, and there is no honest way to measure a normalization change
against it.
