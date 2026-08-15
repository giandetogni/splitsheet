# Schema notes — Phase 0 recon

> **Status.**
> - **Phase 0A — schema, identifiers, normalization, candidate space: COMPLETE** (§1–§7).
> - **Phase 0B — MVP period selection and sizing: COMPLETE** (§9–§11). Period is
>   2026-06, 23 members, 38,199,641 listens, measured rather than extrapolated.
> - **Phase 1A — second verified copy in GCS: COMPLETE** (§12).
> - **Phase 1B — remote Terraform state and budget alert: COMPLETE** (§13, `docs/cost.md`).
> - **Phase 1C — project APIs under Terraform: COMPLETE** (`docs/runbook.md`).
> - **Phase 2A — BigQuery datasets and bronze ingestion: COMPLETE** (§14).
> - **Phase 2B — ingestion contract, failure-safe publication, evaluation firewall: COMPLETE** (§15).
> - **Phase 2C — public CI and read-only integration via WIF: COMPLETE** (`docs/runbook.md`).
> - **Phase 3A — deterministic staged normalization library: COMPLETE** (§16).
> - **Phase 3B — canonical ingestion and staged blocking: COMPLETE** (§17).
> - Not started: scoring, tiers, matching decisions, gold, dbt, rights data, Airflow, Dataproc.

Every number here was produced by the commands in `src/recon/` against the pinned
artifacts below. Nothing is quoted from documentation or memory. Raw data is not
committed; artifacts are identified by URL + size + SHA-256 so any run is verifiable.

Recon date: 2026-07-29/30. Machine: 10-core arm64, 16 GB RAM, **15 GiB free disk**,
measured throughput from `data.metabrainz.org` **11.8–12.9 MB/s**.

## 1. Sources as they actually exist

| | ListenBrainz incremental | ListenBrainz full export | MusicBrainz canonical |
|---|---|---|---|
| Path | `listenbrainz/incremental/listenbrainz-dump-2611-20260730-000002-incremental/` | `listenbrainz/fullexport/listenbrainz-dump-2593-20260712-000004-full/` | `canonical_data/musicbrainz-canonical-dump-20260717-080003/` |
| File | `listenbrainz-listens-dump-2611-…-incremental.tar.zst` | `listenbrainz-spark-dump-2593-…-full.tar` | `musicbrainz-canonical-dump-20260717-080003.tar.zst` |
| Bytes | 188,379,424 | **205,073,162,240** (191 GiB) | 2,320,377,487 |
| SHA-256 | `e0e861e5011fb3624b983b62ac4c36859f9e855bf4f9246a72eb31bedcd70248` | not downloaded | `65796cec3609ad45edfcc6a334cb78cae8a4579430bfbe6b9b54c96cf1566cd5` |
| Payload | JSONL, zstd | Parquet, uncompressed tar | CSV, zstd |
| License | **CC0 1.0** (`COPYING` inside artifact) | — | **CC0 1.0** (`COPYING` inside artifact) |
| `Accept-Ranges` | — | **`bytes` (HTTP 206 verified)** | — |

Both licenses were read out of the artifacts themselves, not from a website.

### Facts that constrain the design

1. **The full export is not partitioned by time.** It is a flat sequence of
   `0.parquet … N.parquet` (~125 MB each, ≈1,640 files inferred from total size).
   There is no `year=/month=` structure, so **a period cannot be selected by path.**
2. **Incremental retention is ~12 days.** At recon time only dumps 2600–2611
   (2026-07-19 → 2026-07-30) existed. Older incrementals are deleted. A pipeline
   built on incrementals **cannot be re-run from source later** — a reproducibility
   defect for a portfolio repo.
3. **`listenbrainz/spark/` contains only feedback dumps**, not listens.
4. **Incremental dumps are organised by submission window, not by listen date.**
   Dump 2611 covers submissions 2026-07-29T00:00:03Z → 2026-07-30T00:00:02Z, yet its
   single member is `listens/2026/7.listens` and its `listened_at` values span
   **258 distinct months** (2005-02 → 2026-07).
5. `SCHEMA_SEQUENCE = 1`.

## 2. Listen record shape (measured)

Top-level fields: `user_id`, `user_name`, `timestamp`, `track_metadata`, `recording_msid`.

- The listen time field is **`timestamp`** (epoch seconds). `listened_at` **never
  appears** (0 of 3,949,921 rows) — the dump differs from the LB API field name.
- `user_name` and `user_id` are present on every row → PII policy applies (§5).
- Two structurally distinct MBID origins exist, confirming they must be counted
  separately: `track_metadata.additional_info.recording_mbid` (client-supplied) and
  `track_metadata.mbid_mapping.recording_mbid` (ListenBrainz's own mapper).
- A **third** origin was not anticipated: `additional_info.lastfm_track_mbid`,
  supplied by the Last.fm importer, of **unverified provenance**.

## 3. Coverage — and why the denominator matters

Dump 2611 contains 3,949,921 listens, 0 parse errors. But **68.4 % of them are bulk
historical backfill** (top client: `ListenBrainz lastfm importer v2`, 55 % of rows), so
rates over the whole dump describe an import batch, not a listening period. The
right-hand column restricts to `listened_at` in 2026-07 and is the basis for design.

| Metric | All submissions (n=3,949,921) | **listened_at 2026-07 (n=1,249,723)** |
|---|---|---|
| `additional_info.recording_mbid` — **Tier A** | 3.46 % | **10.20 %** |
| `additional_info.isrc` — Tier B candidate | 5.50 % | **17.40 %** |
| `lastfm_track_mbid` — unverified origin | 9.41 %¹ | **23.37 %** |
| `mbid_mapping.recording_mbid` | 0.026 % | **0.082 %** |
| no MBID from any trusted origin | 96.52 % | **89.72 %** |
| `duration_ms` | 37.26 % | **49.73 %** |
| `ms_played` | 18.76 %¹ | **0.28 %** |
| track name carries a version marker | 9.41 % | 7.88 % |
| null/blank artist | **0.00 %** | **0.00 %** |
| null/blank track | **0.00 %** | **0.00 %** |
| duplicates on `(user_id, timestamp, recording_msid)` | **0.00 %** | **0.00 %** |
| timestamps outside 2001–2030 | **0** | **0** |
| malformed client `recording_mbid` | **0** | **0** |

¹ whole-dump figure, dominated by importer traffic.

ISRC format validity: 217,291 of 217,440 well-formed (99.93 %).
Cardinality in 2026-07: 20,360 users, 147,726 artist strings, 480,233 distinct
(artist, track) pairs. Raw JSON averages **562.6 bytes/listen**.

### Consequences

- **Tier A covers ~10 %, so the matching problem is real** — ~90 % of a period's
  listens need resolution. The project's premise survives contact with the data.
- **ISRC (17.4 %) outranks Tier A (10.2 %)**, making Tier B the highest-value tier —
  but see §4: the canonical dump has no ISRC.
- **`mbid_mapping` is absent (0.08 %)**, so it cannot serve as the evaluation set.
- **`ms_played` at 0.28 % kills the ≥30 s stream-qualification rule.** Not viable.
- Zero nulls and zero duplicates: `NULL_ARTIST`, `NULL_TRACK` and dedup logic have
  **no naturally occurring failing cases** here and need injected defects to be
  proven — a test that never saw a defect is weak evidence.

## 4. Canonical side (measured)

`canonical_musicbrainz_data.csv` — 7,519,259,059 bytes uncompressed, **31,554,198 rows**,
0 malformed. Columns:

```
id, artist_credit_id, artist_mbids, artist_credit_name,
release_mbid, release_name, recording_mbid, recording_name,
combined_lookup, score
```

- **No ISRC. No length/duration.** Confirmed by header inspection.
- Grain is **one row per `recording_mbid`** (≈31.6 M estimated distinct ≈ row count).
- **`combined_lookup` is effectively unique**: across 491,953 sampled lookups
  (deterministic 1-in-64 keyspace sample), distinct `recording_mbid` per lookup had
  mean 1.0, p99 1, **max 1**. MusicBrainz has already collapsed variants.
- `combined_lookup` normalisation is **inconsistent**: 0 % non-ASCII (everything is
  transliterated) but **4.78 % still contain spaces**, e.g.
  `variousartistsXiang Chou Si Yun ` — mixed case and trailing space retained.
  Replicating it exactly requires their transliteration, not just casefolding.
- `score` ranges 1 … 5,623,672 (semantics not yet established).

Companion files: `canonical_recording_redirect.csv` (860,989,878 B;
`recording_mbid, canonical_recording_mbid, canonical_release_mbid`) — this is the
variant-collapsing map — and `canonical_release_redirect.csv` (624,362,845 B).

**Important limit of the uniqueness result:** it holds for *MusicBrainz's* key. It does
**not** show that an aggressively suffix-stripped key is unique. `Song` and
`Song (Remastered 2017)` are distinct recordings with distinct `combined_lookup`s; our
own normalisation would merge them. The recall-versus-ambiguity trade-off is therefore
still unmeasured, and is the next thing to measure.

## 5. PII handling

`user_name`/`user_id` are on every row. Per project policy the profilers emit
aggregates only; `docs/phase0/*.json` was grep-verified to contain no username as a
value (`user_name` appears only as a schema key). Raw dumps are git-ignored.

## 6. Volume and cost estimates (labelled as estimates)

A month cannot be measured without either the full dump or ~30 incrementals, so these
are extrapolations, not measurements:

- 1,249,723 listens for 2026-07 arrived in one submission day → order **35–40 M
  listens per listening month**, plus a long tail of late arrivals.
- At 562.6 B/listen → **~20 GB raw JSON/month**, ~1.8 GB zstd.
- BigQuery: a trimmed column set should land in single-digit GB; one month sits inside
  the 1 TB/month free query tier and near the 10 GB free storage tier.
- Local disk (15 GiB free) **cannot hold the 191 GiB full dump**; at 11.8 MB/s a full
  download is ≈4.8 h of transfer.

## 7. Candidate space — the measurement that decides the matcher

`src/recon/probe_candidate_space.py` folds both sides to the same key and counts real
candidates. Listen side: `listened_at` 2026-07, 1,249,723 listens. Canonical side: all
31,554,198 rows. Runtime 3 m 42 s.

Our fold **matches the observed MusicBrainz `combined_lookup` key on 90.84 % of the
measured canonical rows.** Parity is not total and must not be described as such. It cannot reproduce the rest because MusicBrainz romanises non-Latin
scripts: **6.79 % of canonical rows** and **3.32 % of listens (41,465)** fold to an
empty key and are unreachable by any string method. That is a hard floor, not a bug.

| | exact key | aggressive (suffixes stripped) |
|---|---|---|
| listens covered | 1,208,258 | 1,203,247 |
| **zero candidates** | **26.02 %** | **20.44 %** |
| **exactly one candidate** | **73.64 %** | **51.93 %** |
| **more than one candidate** | **0.34 %** | **27.63 %** |
| mean candidates / listen | 0.75 | 2.32 |
| p50 / p99 candidates | 1 / 1 | 1 / 25 |
| max candidates for one key | 945 | 459 |
| total candidate pairs | 909,727 | 2,788,222 |

### Aggressive suffix removal is a net loss in this data

Stripping version suffixes moves **5.58 pp** of listens from zero candidates to at
least one — and simultaneously moves **27.29 pp** from exactly one candidate to many.
(The two columns cover slightly different listen sets, 1,208,258 vs 1,203,247, because
a few listens yield one key but not the other. Expressed against the common base of
1,249,723 listens in scope the gain is 68,445 listens, ≈5.5 pp — the conclusion does
not depend on which base is used.)
Listens with exactly one candidate, the only ones a conservative matcher can pay
without further evidence, fall from **73.64 % to 51.93 %**.

The reason is structural, not incidental. When `Song`, `Song (Live)` and
`Song - Remastered 2017` collapse to one key, the surviving candidates differ *only by
the text that was just deleted*. Scoring cannot separate them, because the canonical
dump has **no duration and no ISRC** (§4) — the two signals that would have broken the
tie. Under the project's own rule (ambiguous ties are never paid), aggressive
normalisation therefore converts "no match" into "unresolvable tie" and both land in
the black box.

**Design consequence:** normalisation must be *staged*, not aggressive. Match on the
exact key first; fall back to the suffix-stripped key **only for the 26.02 % that found
zero candidates**. This keeps the 73.64 % clean matches intact and confines all added
ambiguity to the fallback path, where at most 5.58 pp can be gained.

### Spark is not justified by the candidate space

Mean 2.32 candidates per listen, p99 = 25, max 459. Scaled to a month
(≈37 M listens): **≈82 M candidate pairs worst case, ≈27 M on the exact key**. There is
no candidate-set explosion and no skew crisis; the blocking step is an equi-join on a
normalised key, which BigQuery executes as a routine hash join.

The one remaining argument for Spark is Tier D fuzzy scoring — Jaro-Winkler over tens
of millions of pairs is genuinely awkward in BigQuery (JS UDF) and cheap in Spark. But
Tier D's entire upside is capped at the 5.58 pp above, most of which is ambiguous.
That trade has to be settled before Spark is adopted for it.

## 8. Reproducing

```
export DATA_DIR=/path/with/3GB/free
make phase0-fetch        # pinned URLs, verifies published SHA-256
make phase0-profile      # -> docs/phase0/listens_*.json, canonical_profile.json
make phase0-candidates   # -> docs/phase0/candidate_space.json
```

Raw artifacts are never copied into the repo; `DATA_DIR` points wherever they live.
Nothing here touches GCP, so Phase 0 has cost $0.

Verified on 2026-07-30: the full path above was executed end to end (6 m 54 s, exit 0)
and all 21 figures quoted in this document were re-derived byte-identically from the
regenerated `docs/phase0/*.json`. The numbers here are outputs, not transcription.

## 9. Phase 0B — MVP period, measured

Environment: `uv`, Python **3.12.13**, `pyarrow 22.0.0`, exact versions in `uv.lock`
(`requires-python = ">=3.12,<3.13"`, because PySpark does not support the system 3.14).

Wall clock **35.5 min** against a 3 h box. Totals across every Range request made:
**1,744 requests, 117,190,964 bytes = 0.0571 % of the 205,073,162,240-byte archive.**
Nothing was written to disk beyond JSON reports.

### Technique

1. **Member location.** The tar is uncompressed and the server sends
   `Accept-Ranges: bytes`, so members are addressable. A header walk reads 4,096 B per
   member — enough to cover a pax extended header, its payload and the following real
   header in one request. **1,529 members (1,526 Parquet), 1,531 requests, 6,265,856 B,
   25.5 min** → `docs/phase0/fullexport_index.json`.
2. **Footer-only reads work**: 1 request, 65,536 B = **0.05 %** of a 129 MB member,
   yielding row counts, row-group count and schema.
3. **Footer statistics do not exist for `listened_at`.** It is `timestamp[ns]`, and the
   writer (`parquet-cpp-arrow 23.0.1`) omits min/max for nanosecond timestamps — every
   *other* column in the same row group does carry statistics, which confirms this is
   writer behaviour, not a parsing error. Recorded because it is the kind of assumption
   that silently invalidates a design.
4. **Fallback: read the column, not the member.** `listened_at` costs
   **0.45–5.9 % of a member** (589 KB–6.6 MB vs ~125 MB). Cheap enough that footer
   statistics were never needed.

### Binary answer: the export is strictly ordered by `listened_at`

**Yes.** 28 members were measured and are monotonic and contiguous with no overlap:
3.parquet ends `2005-02-17T15:34:08`, 4.parquet begins `2005-02-17T15:34:15`.
Spot samples across the archive: 100→2009-11, 250→2013-08, 400→2016-11, 550→2019-03,
700→2021-01, 850→2022-06, 1000→2023-09, 1150→2024-10, 1300→2025-08, 1450→2026-03,
1520→2026-07-03, 1525 ends `2026-07-12T00:00:01`. Archive spans **2005-01-01 →
2026-07-12**. Rows per member are near-constant (~1.65 M) while the time span per member
falls from ~10 days (2009) to ~1.3 days (2026) — that is traffic growth, and it means
period size must be derived from the index, never assumed.

### Target period 2026-06 — measured, not estimated

| | |
|---|---|
| members | **1495.parquet … 1517.parquet (23 files)** |
| bytes | **2,671,810,509 (2.67 GB) = 1.3029 % of archive** |
| listens | **38,199,641** |
| cost to locate | 16 probes, 86 requests, 17,284,683 B |

Boundary members were counted exactly (1495 contributes 886,038 rows; 1517 contributes
1,473,603). Interior members need no read: because the archive is sorted and contiguous,
any member strictly between the boundaries lies wholly inside the period, so its footer
row count is exact.

This retires the extrapolation in §6: the earlier guess of 35–40 M listens/month was
right, but **38,199,641 is now measured.** 2.67 GB also fits the 11 GiB free disk.

### The Parquet export is a different dataset from the JSONL dump

Schema is 11 columns only — `listened_at, created, user_id, recording_msid, artist_name,
artist_credit_id, release_name, release_mbid, recording_name, recording_mbid,
artist_credit_mbids`. Compared with the JSONL incremental it **has no `isrc`, no
`duration_ms`, no `lastfm_track_mbid`, no `submission_client`, no `additional_info`, and
no `user_name`.**

Coverage measured on 1502.parquet (1,680,000 rows, mid-June 2026):

| column | non-null |
|---|---|
| `recording_mbid` | **85.716 %** |
| `artist_credit_mbids` | 87.059 % |
| `release_mbid` | 85.184 % |
| `artist_credit_id` | 85.207 % |
| `user_id` | 100 % |

**85.7 % versus 10.2 % client-supplied in the JSONL.** The full export therefore carries
ListenBrainz's *mapped* MBID, applied retroactively — the mapper has long since run over
historical listens, whereas same-day incremental submissions showed only 0.08 %.
(Inference from the two coverage figures, not from documentation; it would be confirmed
by joining the same listens across both artifacts.)

### Evaluation policy for `recording_mbid` — binding

> **The ListenBrainz mapper output is a correlated reference label, not independent ground
> truth. Agreement metrics must not be presented as absolute matching accuracy.**

This column is a **ListenBrainz mapper reference label**. It is *not* ground truth and
must never be described as such, in this repository or anywhere else.

Required naming, everywhere — code, reports and documentation:

| use this | never this |
|---|---|
| candidate recall **against mapper reference label** | recall, accuracy |
| unique-candidate **agreement with** mapper reference label | correctness, precision |
| false-unique **disagreement with** mapper reference label | false positive rate, error rate |

- Its provenance is **inferred, not confirmed** (§9, coverage comparison). Even once
  confirmed, it stays a **proxy** for evaluation, carrying the mapper's own errors and
  biases — and those biases are **correlated with ours**, because any string-based
  matcher and the mapper both fail on the same hard inputs. Agreement therefore
  overstates correctness, and disagreement is not automatically our error.
- **Excluded from the matcher entirely**: not an input, not a blocking key, not a
  scoring feature, not a tie-breaker, not a filter. It is readable *only* in the
  evaluation step, after match results are frozen.
- Metrics are reported **only on the labelled subset**, never over all 38,199,641
  listens. Required reporting, always together:
  `label_coverage`, `agreement_vs_mapper`, `recall_vs_mapper`,
  `disagreement_count_vs_mapper`, `abstention_rate`, `matcher_vs_mapper_divergences`, and
  results for the **unlabelled subset reported separately** with no accuracy claim
  attached. The names avoid "precision" and "false positive" deliberately: both imply a
  ground truth that does not exist here.

Any statement of the form "the matcher is N % accurate on 38.2 M listens" is
prohibited: roughly 14.3 % of the period has no reference label at all.

## 10. Preserved slice — 2026-06 (source of record)

Source ratified: **a fixed slice of the full export**, not the ephemeral incremental.

Extracted by HTTP Range directly from the archive; nothing else was downloaded.

| | |
|---|---|
| slice id | `listenbrainz-fullexport-2593-2026-06` |
| members | 1495.parquet … 1517.parquet (23) |
| bytes | **2,671,810,509** transferred, equal to expected |
| requests / retries | **333 / 0** |
| duration | **332.8 s** (≈8.0 MB/s) |
| source ETag | `"6a591543-2fbf502000"` |
| source Last-Modified | `Thu, 16 Jul 2026 17:30:43 GMT` |
| licence | CC0 1.0 (COPYING inside archive) |

Raw members live **outside the repository** at
`~/splitsheet-data/raw/fullexport-2593-2026-06`, mode `0444` (immutable in practice),
and are git-ignored. The committable artifact is
`docs/phase0/period_2026_06_manifest.json` — member names, tar offsets, sizes and
SHA-256 only, no listen data. That manifest is sufficient to re-derive or re-validate the
slice byte-for-byte against the same archive.

### Verification — three independent checks, all passing

`docs/phase0/period_2026_06_verification.json`:

- **size** per member equals the tar index;
- **SHA-256** per member equals the value hashed during download (23/23);
- **row counts** re-derived locally from full file reads: 39,200,000 rows across all
  members, of which **38,199,641 fall inside 2026-06 — exactly the figure measured
  independently by the Range probes.** Boundary members agree to the row
  (1495 → 886,038; 1517 → 1,473,603).
- files confirmed read-only, and members confirmed temporally contiguous with no overlap.

The agreement between a 117 MB Range-probe measurement and a 2.67 GB local re-read is
what makes the 38,199,641 figure trustworthy: two different code paths, same number.

One anomaly worth recording: **1515.parquet holds 2,240,000 rows** where every other
member holds 1,680,000. Member row counts are therefore not uniform and must never be
assumed.

### PII

Raw members carry `user_id` (100 % populated) and **no `user_name`**. `user_id` stays in
the private raw copy only. Any processed, published or committed derivative must exclude
it unless an objective deduplication need is demonstrated and documented — the
deduplication key measured in Phase 0A showed **zero** duplicates, so no such need is
established today.

## 11. Provenance of `recording_mbid` — investigated

### The direct event join is impossible with available artifacts

Attempted and measured, not assumed: 46,382 JSONL events with `listened_at` in 2026-06
were joined against all 39,200,000 slice rows on
`(user_id, listened_at, recording_msid)` — a key both artifacts carry. **Zero matches**
(`docs/phase0/mbid_provenance_probe.json` companion run).

The cause is structural. The export snapshot is 2026-07-12 and contains listens *stored*
by then. Every retained incremental was submitted later (2600 = 2026-07-19 … 2611 =
2026-07-30), and incrementals contain only newly submitted listens. Incrementals from
before the snapshot have been deleted by the ~12-day retention. **No available JSONL dump
can share a single event with the export.** The `sample/` dump does not help: it contains
metadata and Spark caches only, no listens.

### Structural test instead, on the preserved slice

`recording_msid` is ListenBrainz's identifier for an (artist string, track string) pair.
If `recording_mbid` were client-supplied, its presence would depend on *who submitted*,
so a string seen by many users would show both null and non-null values. If ListenBrainz
derives it from the string, it is a function of the msid: all-or-nothing, no conflicts.

Measured over all 39,200,000 rows, sampling 1/8 of the msid keyspace:

| | |
|---|---|
| distinct msids sampled | 844,184 |
| msids seen ≥2 times | 332,607 (mean 13.25 occurrences, max 428,863) |
| all occurrences have an MBID | 252,141 |
| no occurrence has an MBID | 77,559 |
| **mixed null / non-null** | **2,907 = 0.874 %** |
| **conflicting MBID values** | **354 = 0.106 %** |

Client-supplied values cannot behave this way. Only ~10 % of JSONL rows carry a client
MBID, so a string observed by 13 users on average — some by 428,863 events — would show a
mixed rate in the tens of percent. It is 0.87 %.

**Finding: strong structural evidence consistent with ListenBrainz-derived mapping.**
No direct proof exists, because the event-level join was impossible (above). This does
not upgrade the column's status: it remains a **ListenBrainz mapper reference label**
governed by the evaluation policy above.

The residual 0.874 % mixed and 0.106 % conflicting are recorded as **candidates for
upstream changes**, not as mapper revisions. Attributing them to the mapper changing its
answer would require a temporal analysis that first rules out other causes — MBID
redirects, canonical corrections and merges, `is_redirect` resolution, and re-submission
of the same string under a changed upstream entity. Until that analysis exists, the only
claim supported is that these rates are non-zero and measurable. If some part of them
does turn out to be upstream revision, it is the kind of change that would drive
restatement, but that connection is a hypothesis, not a result.

### Two leads recorded, neither resolved

The sample dump contains `lbdump/spark/recording_length.parquet`
(`recording_mbid, length, recording_id, is_redirect`) — the duration §4 declared missing
from the canonical dump. But it holds only **449,620 rows** against ~31.5 M recordings,
because it is a *sample*. It establishes that such an artifact exists, not that a
full-coverage one is available. `lbdump/metadata/recordings_cache.jsonl` carries recording,
artist, release and tag data but **no ISRC and no length**. ISRC remains unavailable
outside the MusicBrainz relational dump.

## 12. Phase 1A — second copy in GCS (executed)

The slice is no longer single-copy. `gs://splitsheet-raw-944054e7`, project
`ss-de-944054e7`, `US-CENTRAL1`, `STANDARD`, created by
`terraform/raw-storage` (its own state, see `versions.tf`).

Object layout is deterministic and keeps the source member names, so an object maps back
to a byte range of a named archive without a lookup table:

```
raw/listenbrainz/fullexport-2593/period=2026-06/1495.parquet … 1517.parquet
raw/listenbrainz/fullexport-2593/period=2026-06/_manifest.json
```

Each object carries custom metadata: `sha256`, `slice_id`, `period`,
`source_tar_offset`, `source_url`, `source_etag`, `license`. GCS stores only MD5 and
CRC32C natively, so without this the manifest's SHA-256 would not be checkable from the
cloud side.

Upload: **23 objects + manifest, 2,671,810,509 bytes, 0 retries, 0 failures, 198.6 s.**
Every object's MD5 was verified against the local file after upload, and the service was
asked to reject a corrupt transfer rather than store it (`checksum="md5"`).

Verification (`docs/phase0/gcs_verification.json`), run against the cloud copy on its own
terms rather than trusting the upload report: 23/23 objects, no extras, all sizes match,
all SHA-256 metadata matches the manifest, all MD5 match the local files,
`public_access_prevention=enforced`, uniform access on, **no public IAM members**, and
**38,199,641 in-period rows re-derived from the cloud objects** — the same figure produced
independently by the Range probes and by the local re-read. Cost: 2,891,884 bytes egress.

### Destruction guards — what was actually tested, and what it does not prove

Two mechanisms were exercised. They are **not** two independent guarantees of
immutability, and describing them that way would overstate the protection:

1. **`lifecycle.prevent_destroy = true` blocks destruction via Terraform.**
   `terraform destroy -auto-approve` exits **1** with *"Instance cannot be destroyed …
   has lifecycle.prevent_destroy set"*. Planning fails, so nothing is attempted. This
   guard is scoped to Terraform: it says nothing about actions taken outside it.
2. **`force_destroy = false` stops the provider from deleting the bucket's objects
   automatically.** It is a guard against Terraform emptying the bucket on its way to
   removing it — not a lock on the objects themselves.
3. **The direct `gcloud storage buckets delete` failed only because the bucket was not
   empty.** That is GCS's ordinary refusal to delete a non-empty bucket, not a property
   this configuration added.

**The limit this leaves:** anyone with sufficient IAM permissions can delete the objects
first and then remove the bucket. Nothing here prevents that sequence. Object versioning
and GCS soft delete widen the recovery window after such a deletion, but they are
recovery, not prevention.

Observed after both attempts: 24 objects still present, 2,671,816,509 bytes, resource
still in state. What is demonstrated is that a *careless* single command does not remove
the data — not that the data is immutable.

`user_id` remains in the raw objects and the bucket is private; it must not appear in any
published derivative.

## 13. Phase 1B — remote state and budget

Three Terraform root modules, each with its own state, sharing one backing bucket under
distinct prefixes so no module can read or overwrite another's state:

```
gs://splitsheet-tfstate-944054e7/raw-storage/default.tfstate
gs://splitsheet-tfstate-944054e7/governance/default.tfstate
terraform/bootstrap-state -> local state, by necessity (it creates the bucket above)
```

State bucket, confirmed by `gcloud storage buckets describe` rather than Terraform
output: `US-CENTRAL1`, `STANDARD`, uniform access on, public access prevention
`enforced`, **no public IAM members**, **versioning True**, **soft delete 2,592,000 s
(30 days)**, and two lifecycle rules that delete only noncurrent generations
(`isLive: false`) at 20 newer versions or 180 days — so state history is recoverable
without growing without bound.

Migration of `raw-storage`: the local state was copied to
`~/splitsheet-data/tfstate-backups/` (outside the repo, mode `0444`) and its SHA-256
recorded and re-verified as identical *before* `terraform init -migrate-state` ran. After
migration the remote object is 4,567 bytes — the same size as the original — and
`terraform plan` reports **No changes**.

The raw bucket was not touched: 24 objects, 2,671,816,509 bytes, and `1495.parquet` still
has exactly one generation, so nothing was rewritten.

Budget: see `docs/cost.md`. It is an alert, not a cap.

## 14. Phase 2A — BigQuery bronze ingestion

### Correction to the reconciliation criterion

An earlier report proposed `COUNT(*) = 38,199,641` as the Phase 2 acceptance test. That
was wrong: loading the 23 members directly yields **39,200,000**. The correct statement
has three numbers, and the difference is a real classification, not a rounding error:

| | rows |
|---|---|
| `source_total_rows` (all 23 members) | **39,200,000** |
| `in_period_rows` `[2026-06-01, 2026-07-01)` | **38,199,641** |
| `outside_target_period_rows` | **1,000,359** |
| unclassified / null timestamp | **0** |

The 1,000,359 live entirely in the two boundary members (1495 holds May listens,
1517 holds July listens). They are **valid listens outside the target period**, classified
as `OUTSIDE_TARGET_PERIOD` and reconciled — never silently dropped. Reaching 38,199,641
requires an explicit half-open filter, which the loader applies and asserts before writing.

### Timestamp compatibility probe (run before any load)

`listened_at` is **INT96**, not `TIMESTAMP_NANOS` — the legacy nanosecond encoding written
by `parquet-cpp-arrow 23.0.1`. That also explains the absent footer statistics found in
Phase 0B. BigQuery maps INT96 to `TIMESTAMP`.

Truncation risk was measured, not assumed: across 1,680,000 rows of `1502.parquet`,
**0 values carry a sub-microsecond part and 0 carry a sub-second part** — every listen is
a whole second, so BigQuery's microsecond precision is lossless here.

Disposable external table over one file, job
`bqjob_r43a9a3682ad42bbd_0000019fbf6ed69e_1`: inferred type `TIMESTAMP REQUIRED`,
row_count 1,680,000, min `2026-06-09 18:37:47`, max `2026-06-11 02:59:07` — identical to
the local pyarrow read — `sub_second_values = 0`, formatted to 9 fractional digits as
`.000000000`. 13,440,000 bytes processed, 2,089 slot-ms, 891 ms, no errors. Table dropped.

**Probe passed, so the load proceeded.**

### Deduplication key — measured, not assumed

Reused the Phase 0 candidate key `(user_id, listened_at, recording_msid)`. Over the
38,199,641 in-period rows: **38,199,641 distinct keys, 0 colliding groups, max 1 row per
key.** Exact duplicates and distinct-events-sharing-a-key are therefore both zero, and
**no row was removed** — deduplication is unnecessary here, which is a finding rather
than an assumption. Only after proving zero collisions was `listen_hash` defined as
`SHA256(user_id | listened_at_micros | recording_msid)`.

### Column set — mapper-derived fields excluded, not just one

Measured coverage over the period separates raw from derived:

| column | coverage | verdict |
|---|---|---|
| `artist_name` | 100 % | raw |
| `recording_name` | 100 % | raw |
| `release_name` | 97.154 % | raw (11.91 % of rows have it with no MBID at all) |
| `recording_mbid` | 85.863 % | ListenBrainz-derived |
| `artist_credit_mbids` | 86.057 % | ListenBrainz-derived |
| `artist_credit_id` | 85.338 % | ListenBrainz-derived |
| `release_mbid` | 85.265 % | ListenBrainz-derived |

The instruction named `recording_mbid`. The same reasoning applies to its three siblings —
they cluster at the same coverage because they come from the same mapping process — so
**all four are excluded from bronze**. Admitting any of them would hand the matcher a
resolved identifier through a side door.

`bronze_listens` columns: `listen_hash, listened_at, submitted_at, recording_msid,
artist_name, recording_name, release_name, source_file, dump_id, ingestion_run_id,
ingested_at`. No `user_id`, no derived identifier. Partitioned DAY on `listened_at`,
`require_partition_filter = true`, no clustering.

`splitsheet_eval.mapper_reference_labels` holds only `listen_hash`,
`mapper_recording_mbid`, `label_available`. **Label coverage: 85.8626 %** (32,799,203 of
38,199,641). Metrics may only ever be reported over that labelled subset.

### `listen_hash` is pseudonymisation, not anonymisation

It is `SHA256` over a tuple containing `user_id`. The user-id space is small, so anyone
holding the table and a candidate `(listened_at, recording_msid)` could brute-force the
original `user_id`. It removes the raw identifier and gives a stable join key; it does not
make the data anonymous. The datasets are private and must stay that way.

### Idempotency: deterministic replacement, proven by re-run

Chosen: pre-check, then truncate-and-reload. Rejected: `MERGE` on `listen_hash` — it would
join 38.2 M against 38.2 M every run and still need a partition predicate to satisfy
`require_partition_filter`, buying nothing when the source is immutable and the key has
zero collisions. What would change it: late arrivals into a published partition, multiple
slices writing the same table, or partial partition coverage.

| | run A | run B |
|---|---|---|
| inserted | 38,199,641 | **0** |
| skipped | 0 | **38,199,641** |
| failed | 0 | 0 |
| bronze rows | 38,199,641 | 38,199,641 |
| distinct `listen_hash` | 38,199,641 | 38,199,641 |
| labels present | 32,799,203 | 32,799,203 |

Row count did not grow, no duplicate hashes, labels unchanged, reconciliation identical.

An unfiltered `SELECT COUNT(*)` on `bronze_listens` fails with *"Cannot query over table"* —
the partition filter is enforced, not merely configured.

## 15. Phase 2B — ingestion contract, failure-safe publication, evaluation firewall

### The three defects in the Phase 2A loader, answered plainly

| question | answer |
|---|---|
| Did it TRUNCATE before the new state was ready? | **Yes.** `TRUNCATE` then `INSERT` — the empty table was a published state. |
| Could a failure between bronze and eval publish inconsistent states? | **Yes.** Four independent statements; a crash after the bronze insert left labels empty. |
| Was the swap atomic? | **No.** No transaction, no staging. |

All three are fixed.

### Source contract — no wildcard

The external table's `source_uris` is now the explicit 23-URI list generated from
`period_2026_06_manifest.json` inside Terraform (`jsondecode(file(...))`), with a
`check` block that fails the plan if the manifest stops describing exactly 23 members.
A glob would silently absorb any future `.parquet` under the prefix, meaning table
contents could change with no code change.

Before every run the loader re-verifies against GCS and **aborts before querying or
publishing** if anything diverges: object count is 23, no unexpected objects, no missing
objects, per-object size, per-object SHA-256 (from custom metadata), and the total byte
sum of 2,671,810,509.

### Failure-safe publication

1. materialise `stg_bronze_listens_<run>` and `stg_labels_<run>`, targets untouched;
2. validate staging completely — row count, hash uniqueness, no null hash, label row
   count, bronze↔labels alignment, exact schema, no banned column;
3. publish both inside **one BigQuery multi-statement transaction**
   (`BEGIN … DELETE … INSERT … DELETE … INSERT … COMMIT`), so bronze and labels can never
   be left on different runs;
4. drop staging **only after** commit — a failed run leaves it behind on purpose, named by
   run_id, and `--cleanup-orphan-staging` removes it while printing every table dropped.

**Injected-failure proof.** Ran with `--fail-before-publish` after staging validation
passed. Exit code 1. Published state before and after:

| | before | after |
|---|---|---|
| bronze rows | 38,199,641 | **38,199,641** |
| label rows | 38,199,641 | **38,199,641** |
| labels present | 32,799,203 | **32,799,203** |
| rows joining bronze↔labels | 38,199,641 | **38,199,641** |

Unchanged and mutually consistent. Staging was left behind and then cleaned up auditably.

### Evaluation firewall

`splitsheet-matcher@…` — a dedicated service account with **no JSON key**; callers
impersonate it (`roles/iam.serviceAccountTokenCreator` on the human identity only).
IAM propagation took 237 s, which is worth knowing before assuming a binding failed.

Permissions are `roles/bigquery.jobUser` on the project plus `dataViewer` **on the view
alone** — not on the bronze dataset. The view is authorised on `splitsheet_bronze` so it
can read `bronze_listens` on the caller's behalf without the matcher having any access to
that table.

Measured, as the matcher identity:

| target | result |
|---|---|
| `splitsheet_bronze.v_matcher_input` | **ALLOWED** — 38,199,641 rows |
| `splitsheet_eval.mapper_reference_labels` | **Access Denied** |
| `splitsheet_bronze.bronze_listens` | **Access Denied** |
| `splitsheet_bronze.ext_listens_2026_06` | **Access Denied** |

The last two matter as much as the first: `bronze_listens` carries lineage columns and the
external table still carries all four ListenBrainz-derived identifiers. Granting the
dataset instead of the view would have exposed both.

`v_matcher_input` projects exactly nine columns: `listen_hash, listened_at, artist_name,
recording_name, release_name, recording_msid, source_file, dump_id, ingestion_run_id`.

### Tests

**14 integration tests** in `tests/integration/`, marked `integration`, requiring GCP and
ADC — they are not unit tests and `make test` never runs them. They cover the
reconciliation identity, hash uniqueness, banned-column absence, exact view schema,
partition-filter rejection, the firewall (parametrised over all three forbidden tables),
URI/manifest equality including checksums, idempotent re-run, and the injected-failure
survival. **4 unit tests** cover the pure logic idempotency depends on: the reconciliation
constants summing, allowed/banned column sets being disjoint, and run-id determinism
changing if and only if slice content changes.

### Hygiene

`bronze_load_run1.json` moved to `docs/phase0/aborted-runs/` with a README explaining it
reported `skipped` only because the tables had been populated by manual inserts, so it
never exercised the insert path. It is not evidence of anything and is no longer on the
evidence path.

## 16. Phase 3A — deterministic staged normalization

A pure library (`src/normalization/`): no I/O, no clock, no randomness, no GCP. It produces
values, keys and statuses; it does not match, block or score.

### Three outputs per field, and they are not interchangeable

Conflating them was the defect this module was rewritten to fix. An ASCII lookup key is a
*blocking artefact*, not "the normalized value".

| output | what it is |
|---|---|
| `*_normalized_unicode` | the real normalized value, **script-preserving**: NFKD, combining marks dropped, casefolded, Unicode punctuation to space, whitespace collapsed. Cyrillic stays Cyrillic, CJK stays CJK. |
| `*_lookup_exact` | an ASCII-only key. ASCII **only** because that is what matches the observed MusicBrainz `combined_lookup` convention. |
| `*_lookup_fallback` | the aggressive key, usable **only** after the exact key has failed. |

A Cyrillic title normalizes perfectly well and is `VALID`. It simply has no ASCII key.

### Status is independent of key availability

| `normalization_status` | meaning |
|---|---|
| `VALID` | content is usable |
| `MISSING_ARTIST` / `MISSING_RECORDING` | field absent or blank |
| `NO_ALPHANUMERIC_CONTENT` | no letters or digits in **any** script (e.g. `"!!!"`) |

| `exact_key_status` / `fallback_key_status` | meaning |
|---|---|
| `AVAILABLE` | both halves folded; a combined key is emitted |
| `PARTIAL` | exactly one half folded; **no key is emitted** |
| `EMPTY` | neither half folded |

`PARTIAL` exists because a key built from one half would equal the artist alone, collapsing
every unromanisable track by that artist into a single bucket — a silent false-positive
generator. Nothing downstream may block on a `PARTIAL`.

### Years are removed only inside recognised structures

A bare four-digit strip destroys real titles. Removal now requires a configured reissue
structure (`remastered 2017`, `2017 remaster`, `anniversary edition 2017`, `… reissue`),
and version markers are stripped **only when something precedes them**, because a version
marker is by definition a suffix.

Preserved, with tests: `1999`, `1984`, `2001`, `Class of 1984`, **`Live 2000`**,
`Live at Leeds`, `Mono`, `2112`. Every transformation applied is recorded in
`transformations_applied`, including `reverted:would_empty_title` when the fallback would
have destroyed a title outright.

### Parity re-run with the production library

`src/recon/normalization_parity.py` re-ran the Phase 0A experiment using **only**
`src/normalization`, on local files, with no GCP call. 1,249,723 listens, 499,433 distinct
pairs, all 31,554,198 canonical rows, 33 min.

**The exact stage reproduces Phase 0A to the row:**

| | Phase 0A probe | production library |
|---|---|---|
| exact keys identical | — | **100.00 %** |
| listens with an exact key | 1,208,258 | **1,208,258** |
| exact unique-candidate count | 889,716 | **889,716** |
| exact unique %, same denominator | 73.6363 % | **73.6363 %** |
| exact zero %, same denominator | 26.0246 % | **26.0246 %** |

Over *all* 1,249,723 listens the same figure reads 71.1931 %, purely because the
denominator now includes listens that have no key at all. Both numbers are correct; only
one is comparable to the baseline, and quoting the wrong one would be an accidental
regression claim.

**What the patch newly reveals.** Phase 0A could only say 3.32 % had "no usable key".
That splits into **PARTIAL 2.1949 %** and **EMPTY 1.1230 %** (sum 3.3179 %, matching), and
content validity is now measured separately: **99.968 % VALID**, only 0.032 %
`NO_ALPHANUMERIC_CONTENT`. Non-Latin listens are no longer miscounted as invalid.

**Staged beats blanket, confirmed with production code.**

The figure below is **unique-candidate coverage**: the share of listens with exactly one
blocking candidate. It is emphatically *not* a match rate, an attribution rate or a payout
rate. Nothing has been matched yet: there is no scoring, no threshold, no rights data and
no payout anywhere in this project.

| | % of all listens with exactly one blocking candidate |
|---|---|
| exact stage | 71.1931 |
| + gained by staged fallback | 3.2025 |
| **= staged unique-candidate coverage** | **74.3956** |
| blanket aggressive, same measure | 49.8402 |
| **staged advantage** | **24.56 pp** |

A single candidate means only that blocking narrowed the field to one row. Whether that row
is correct is unknown until evaluation, and whether it is payable is several phases away.

The fallback key matches the legacy probe on 91.4316 % of listens; the 8.57 % difference is
the deliberate change — restricted year handling, suffix-position anchoring, and the
revert-if-empty guard. Blanket aggressive now scores 49.8402 % against the old 51.9275 %:
**2.09 pp lower, and that is the price of not destroying titles like `Live 2000`.** It is a
cost, it is paid knowingly, and it is far smaller than the 24.56 pp the staging gains.

**By script group** — the transliteration gap, quantified:

| script | listens | share | exact unique | exact zero |
|---|---|---|---|---|
| Latin | 1,199,620 | 95.99 % | 72.99 % | 26.71 % |
| CJK | 23,970 | 1.92 % | 1.40 % | 97.99 % |
| NoLetters | 15,717 | 1.26 % | 86.85 % | 11.10 % |
| Cyrillic | 8,983 | 0.72 % | 0.75 % | 98.60 % |
| Hebrew / Thai / Arabic / Greek | 1,090 | 0.09 % | ≤2.03 % | ≥97.3 % |

Non-Latin scripts are close to unmatchable by an ASCII key — 2.7 % of listens, essentially
all of them landing in the black box for want of a transliteration table. That is now a
measured, named limitation rather than an invisible one.

### Version cannot drift from the rules — demonstrated, not asserted

`normalization_version` is `<semantic>+<12-hex digest of the effective rules>`, currently
**`1.0.0+0bc0dd643e06`**, and it is pinned in `tests/unit/test_normalization.py`.

Proven by mutation: adding one suffix to the YAML without touching the pin fails
`test_version_matches_the_pinned_value`, reporting the new digest `1.0.0+353dee247928`;
reverting restores green. Reordering a list does **not** change the version, because
reordering is not a behavioural change.

### Tests

**163 unit tests**, table-driven, credential-free, running in the public CI. Covering CJK,
Cyrillic, Greek, Arabic, Hebrew, Hangul, composed vs decomposed Unicode, mixed
Latin/non-Latin pairs, punctuation-only input, numeric-title preservation, word-boundary
safety, and invariants over a fixed corpus (idempotence, no partial key, valid content
never empties, exact never applies fallback rules).

Earlier mutation probes confirmed the suite is not decorative: making the exact stage
aggressive fails 4 tests; removing word-boundary anchoring fails 5.

### One optimisation added, measured, and removed

A regex pre-filter was added to skip the fallback transforms when nothing could match. It
measured **slower** (0.18 s vs 0.02 s per 22,000 strings) *and* wrong on 3 cases, so it was
deleted the same sitting. Recorded because it is the B.6 rule working as intended: the
justification for keeping code is a number, not the effort already spent on it.

## 17. Phase 3B — canonical ingestion and staged blocking (partial)

### Canonical snapshot

Grain **measured before** the table was defined, not assumed: 31,554,198 rows,
31,554,198 distinct `recording_mbid`, **0 repeats, 0 rows without an MBID, 0 exact
duplicates**, `combined_lookup` 100 % (one blank), blocking fields 100 %. Phase 0A had
only a 1-in-64 estimate. Because repeats were zero, the planned second characterisation
pass had nothing to examine and was skipped rather than run for form.

Landed at `gs://…/raw/musicbrainz/canonical/snapshot_date=2026-07-17/…csv.gz`,
1,992,824,873 B, sha256 `8de737f4…`, manifest committed, **bound by explicit URI, no
wildcard**. `bronze_canonical_recordings` loaded with all 31,554,198 rows and reconciled
against the local measurement exactly.

**Two BigQuery ingestion faults worth recording.** The first read failed at a byte offset
that looked like binary; I wrongly blamed quoted newlines and added
`allow_quoted_newlines`, which failed earlier still. The real cause was
`compression: NONE` — the provider does not infer GZIP from the extension, so BigQuery was
parsing gzip bytes as text. Setting `compression = "GZIP"` fixed it and the unjustified
setting was **removed**. Separately, the mapping TSV genuinely does need
`allow_quoted_newlines`: it contains **790 extra physical lines** from newlines inside
titles. Same-looking symptom, two different causes; only measurement separated them.

### Normalization of the June corpus

Distinct-tuple strategy, as required: **38,199,641 listens carry 4,599,791 distinct
(artist, recording) pairs — 8.30×** less normalization work, ~5 min instead of ~40. The
rules stay in one Python implementation and are never re-expressed in SQL; the mapping
table is how their output reaches BigQuery.

The builder asserts its distinct-pair count against BigQuery's, **and that assertion
caught a real error**: the first run read the local slice unfiltered, taking all
39,200,000 rows instead of the 38,199,641 in period.

`silver_listens_normalized`: **38,199,641 rows, listen_hash unique**. `release_normalized_*`
is deliberately absent — nothing in this phase uses it.

### Staged blocking

Precedence enforced and verified: EXACT for every listen with an AVAILABLE exact key;
FALLBACK **only** for listens with zero EXACT candidates. Validated before publication:
grain unique, no candidate from an empty key, and **`listens_with_both_stages = 0`**.

Generated entirely as the matcher service account, which is denied `splitsheet_eval` and
`bronze_listens` — re-proved after the IAM change. The candidate table is label-blind by
construction, not by convention.

| candidate-space, June | |
|---|---|
| listens | 38,199,641 |
| exact eligible | 36,584,394 |
| exact zero / one / many | 5,030,438 / **31,421,104** / 132,852 |
| exact candidates, mean per eligible | 32,354,243, **0.8844** |
| exact p50 / p90 / p95 / p99 / max | 1 / 1 / 1 / 1 / **945** |
| fallback executed | 536,546 (one 319,001, many 217,545, max 476) |
| zero after both stages | 6,109,139 |
| **total candidate pairs** | **34,466,312** |

Unique-candidate coverage — *not* a match rate, nothing is matched: **82.25 % exact plus
0.84 % fallback = 83.09 %**, against July's 74.40 %. The corpora differ materially, which
is exactly the risk flagged at the end of Phase 3A; July is a submission-day dump that is
68 % historical backfill, June is a real listening period.

### Evaluation on the frozen split

Labels were read only after candidates were published, under the human identity, using the
split frozen before any metric existed.

| | dev | holdout |
|---|---|---|
| evaluable listens | 25,727,285 | 5,504,853 |
| **candidate recall against mapper reference label** | **95.5153 %** | **96.5356 %** |
| unique-candidate agreement with mapper reference label | 99.9853 % | 99.9818 % |
| **false-unique disagreement with mapper reference label** | **0.0147 %** | **0.0182 %** |
| zero-candidate | 4.4637 % | 3.4390 % |
| multi-candidate | 0.3504 % | 0.7835 % |
| fallback recall | 77.7176 % | 77.4645 % |
| fallback false-unique | 2,664 | 774 |

Dev and holdout agree closely, and the holdout was used for nothing except this table.

Two classes are reported separately and are **not** blocking failures:
`NOT_EVALUABLE` **5,400,438** listens with no label, and
`LABEL_NOT_IN_CANONICAL_SNAPSHOT` **1,567,065** listens whose label does not exist in the
snapshot — blocking cannot propose a candidate that is not in its index.

### BigQuery versus Spark — decided on the numbers

**Chosen: BigQuery SQL. Rejected: Spark/Dataproc.**

- total candidate pairs **34,466,312** for 38.2 M listens — mean **0.90** per listen;
- exact p99 = **1**; largest single block 945;
- whole staged build ran in **77.2 s**, 42.75 GB billed, 5,171,812 slot-ms;
- no compute here is awkward in SQL: blocking is one equality join per stage.

Nothing about this is Spark-shaped. Evidence that would change it: candidate pairs in the
billions, a p99 in the thousands, a runtime in hours, or Tier-D fuzzy scoring over tens of
millions of pairs, which is genuinely awkward in BigQuery. Spark is **not** implemented.

### Idempotency and failure safety — proven, not asserted

| | before | after re-run | after injected failure |
|---|---|---|---|
| candidates | 34,466,312 | **34,466,312** | **34,466,312** |
| distinct `candidate_run_id` | 1 | **1** | **1** |
| normalized rows | 38,199,641 | **38,199,641** | **38,199,641** |

The re-run produced the identical `candidate_run_id` `blk:c005e9a56b1ec542`, because the id
is derived from source and config rather than from wall-clock time. The injected-failure
run exited **1** after staging validation and before publication, left staging behind for
audit, and changed nothing that was published.

### June corpus by script

| script | listens | share | exact AVAILABLE | PARTIAL | EMPTY |
|---|---|---|---|---|---|
| Latin | ~35,826,000 | ~93.79 % | high | low | low |
| CJK | 1,472,011 | 3.85 % | — | — | — |
| Other | 602,914 | 1.58 % | 96.93 % | 2.85 % | 0.22 % |
| Cyrillic | 264,884 | 0.69 % | **7.86 %** | 47.19 % | 44.95 % |
| Greek | 12,451 | 0.03 % | 36.43 % | 26.58 % | 36.99 % |
| Arabic | 10,316 | 0.03 % | 37.49 % | 50.50 % | 12.01 % |
| Hebrew | 6,143 | 0.02 % | 8.97 % | 53.41 % | 37.62 % |
| Thai | 4,743 | 0.01 % | 47.71 % | 46.43 % | 5.86 % |

Content validity stays ≥97.8 % in every script — non-Latin listens are **valid**, they
simply lack an ASCII key. Cyrillic is the clearest case: 7.86 % have a usable exact key
while 92 % are PARTIAL or EMPTY. June is also markedly more international than July
(CJK 3.85 % against 1.92 %), which is part of why the two corpora differ.

### Tests

11 integration tests for blocking: normalized grain, no PII or derived identifier in
silver, **fallback never runs where exact hit**, unique candidate grain, no candidate from
an EMPTY or PARTIAL key, every candidate exists in the canonical snapshot, candidate table
label-blind, matcher still denied on eval after gaining silver write, and
`LABEL_NOT_IN_CANONICAL_SNAPSHOT` kept as its own class. Two are marked
`integration_destructive` — idempotent re-run and injected-failure survival — and never
run in CI.


## 18. Phase 3B patch — block analysis and the fallback trade-off

Read-only. No rule, table, IAM binding or infrastructure was changed.

### The 945-candidate exact block is destructive normalization, not a homonym

Key **`cvver`**, 945 canonical recordings, 460 listens, 434,700 candidate pairs.

The members are Japanese titles such as `感情アクセラレイション -早乙女彩華ソロver.-` by
`ミシェル・イェーガー(CV.市ノ瀬加那)`. Folding to ASCII discards every CJK character, and the
only survivors are the Latin fragments embedded in them — `CV` from the voice-actor credit
and `ver` from the version marker. Every such recording collapses onto `cvver`.

This is a defect class the current statuses do not catch. The key is not `PARTIAL` and not
`EMPTY`: both halves contain *some* ASCII, so the pair is `AVAILABLE` while being
semantically empty. **A key can be structurally valid and still carry no information.**

Recorded, not fixed: adding a guard here would be a new rule, which this patch excludes.

### The largest blocks by pairs are mostly benign volume

Nine of the top ten exact blocks are single-recording BTS tracks — `btsswim` alone is
2,038,476 listens against **1** canonical recording, so 2,038,476 pairs and zero ambiguity.
**Largest block is not the same as most skewed.** Only `cvver` is a genuine collision.

### The largest fallback blocks are episodic content and version proliferation

| key | canonical recordings | listens | pairs | cause |
|---|---|---|---|---|
| `abovebeyondgrouptherapy` | 476 | 227 | 108,052 | radio show; `[ABGT262]`, `[ABGT286]` … strip to one key |
| `arminvanbuurenstateoftrance` | 455 | 144 | 65,520 | same, episodic show |
| `beatlesstrawberryfieldsforever` | 225 | 198 | 44,550 | genuine version proliferation: demos, takes, mixes |
| `beatlestwistandshout` | 138 | 150 | 20,700 | same |
| `btsswim` | 12 | 6,354 | 76,248 | remix/acoustic variants |

Episodic content is the interesting case: bracketed episode identifiers are exactly what
the fallback strips, so a show with hundreds of episodes becomes one key. That is the
fallback working as designed and being wrong for this content type.

### The fallback trade-off, quantified — with numerators and denominators

Both buckets, listens with **zero** exact candidates that have an evaluable reference label:

| | dev | holdout |
|---|---|---|
| reached fallback (denominator) | 1,168,119 | 194,668 |
| fallback key unavailable | 1,040,247 (89.1 %) | 167,037 (85.8 %) |
| fallback zero candidates | 108,130 | 22,275 |
| fallback exactly one | 11,601 | 3,268 |
| fallback multiple | 8,141 | 2,088 |
| reference found by fallback | 15,343 | 4,149 |
| **incremental recall against mapper reference label** | **1.3135 %** | **2.1313 %** |
| unique agrees with reference | 8,937 | 2,494 |
| unique disagrees with reference | 2,664 | 774 |
| **false-unique disagreement rate** | **22.9635 %** | **23.6842 %** |
| multi-candidate containing the reference | 6,406 | 1,655 |
| multi-candidate missing the reference | 1,735 | 433 |
| mean / p50 / p90 / p95 / p99 / max candidates | 0.0533 / 0 / 0 / 0 / 1 / 145 | 0.0791 / 0 / 0 / 0 / 2 / 107 |

### Answers

**Does the fallback add enough recall to justify keeping it?** It adds **1.31 % (dev) /
2.13 % (holdout)** incremental recall on the listens that reach it — 15,343 and 4,149
references found. Modest but real.

**What share of the gain comes from unique candidates?** 8,937 / 15,343 = **58.2 %** (dev),
2,494 / 4,149 = **60.1 %** (holdout).

**What share ends in ambiguity?** Of listens that got any fallback candidate,
8,141 / 19,742 = **41.2 %** (dev) and 2,088 / 5,356 = **39.0 %** (holdout) are
multi-candidate.

**Is there evidence to remove, restrict or keep it unchanged?** The decisive number is the
**false-unique disagreement rate of 22.96 % / 23.68 %** — against **0.0147 % / 0.0182 %**
for the corpus as a whole, roughly **1,500× worse**. Nearly one in four fallback listens
that look unambiguous disagrees with the reference. Those are precisely the rows a naive
downstream stage would accept without scrutiny, and under this project's stated principle —
paying the wrong rights holder is worse than suspending payment — that is the dangerous
shape.

The evidence supports **restricting, not removing**: the recall is real, and 89 % of listens
reaching the fallback have no usable fallback key anyway, so the tier is small. What it does
not support is treating a lone fallback candidate as confident. That is an input to scoring
design, and the holdout agrees with dev on every direction, so the conclusion is not an
artefact of one split.

**No rule was changed.** No salting, no exception list, no blacklist.

### Cost of this patch

Two analytical queries, dry-run estimates 11,654,921,446 and 12,079,772,161 bytes, both run
under `maximum_bytes_billed`. List-price equivalent well under $0.20; **actual monetary cost
UNKNOWN without billing evidence**, and consumption remains inside the monthly free query
allowance.

## 19. Phase 4 preflight — ASCII residual and low-information keys

Read-only. No rule, status, table, IAM binding or infrastructure changed. The label
`POTENTIAL_LOW_INFORMATION_KEY` is **experimental and analytical only**; it exists nowhere
in production and affects no candidate. The mapper reference label took no part in defining
it.

### Signals

Per distinct pair in `listen_pair_normalization` (4,599,791 rows, the natural universe):
`unicode_alnum_length`, `ascii_lookup_length`, `ascii_retention_ratio`, per-script character
counts, surviving ASCII token count and max token length, block cardinality, listens and
candidate pairs.

### Sensitivity across cuts — exact stage, keys `AVAILABLE`

| `ascii_retention_ratio` | keys | listens | % of 38,199,641 | cand. pairs* | % of 34,466,312 | mostly non-Latin | only short tokens (≤3) | card ≥10 | card ≥100 | card max |
|---|---|---|---|---|---|---|---|---|---|---|
| < 0.05 | 28 | 33 | 0.0001 % | 92 | 0.0003 % | 28/28 | 25 | 1 | 0 | 19 |
| < 0.10 | 122 | 227 | 0.0006 % | 239 | 0.0007 % | 122/122 | 99 | 5 | 0 | 28 |
| < 0.20 | 793 | 2,046 | 0.0054 % | 314,175 | 0.9115 % | 791/793 | 396 | 114 | **82** | **945** |
| < 0.30 | 1,898 | 8,409 | 0.0220 % | 165,115 | 0.4791 % | 1,892/1,898 | 485 | 120 | 68 | 945 |
| ≥ 0.30 | 4,257,771 | 36,573,679 | 95.7435 % | 31,874,622 | 92.4805 % | 8,184 | 7,911 | 1,124 | 117 | 715 |

\* Band pair counts are reconstructed as `listens × block cardinality` and **undercount**
against the candidate table: the band containing `cvver` reconstructs to 314,175 where the
candidate table holds 434,700 for `cvver` alone. Treat the band figures as lower bounds and
the candidate-table figures as authoritative.

Cumulative for ratio < 0.30: **2,841 keys, 10,715 listens (0.028 %)**, and on the order of
**1.4 % of candidate pairs**.

Note that `mostly_non_latin` is essentially 100 % in every low band — the phenomenon is
entirely non-Latin content leaving Latin residue. `single_ascii_token` was 0 everywhere and
proved useless as a signal, because the residue is typically two fragments, not one.

### `cvver` reproduced

144 distinct pairs, **`ascii_retention_ratio` 0.1957**, mean 26.3 Unicode alphanumerics
reduced to 5 ASCII characters, mean 20.8 CJK characters discarded. Example normalized value:
`チノ cv 水瀬いのり しんかーそんくはやほやメロティー チノver` — the survivors are the voice-actor
marker `cv` and the version marker `ver`. Block cardinality 945, 460 listens,
**434,700 candidate pairs** from a single key.

### The finding I did not expect

| band | bucket | evaluable | recall vs reference | unique agreement | false-unique disagreement | multi-candidate |
|---|---|---|---|---|---|---|
| low-info < 0.20 | dev | 739 | 98.5115 % | **100.0000 %** | **0.0000 %** | **67.2530 %** |
| low-info < 0.20 | holdout | 223 | 99.5516 % | **100.0000 %** | **0.0000 %** | **33.1839 %** |
| normal ≥ 0.20 | dev | 24,686,299 | 99.5401 % | 99.9853 % | 0.0147 % | 0.3632 % |
| normal ≥ 0.20 | holdout | 5,337,593 | 99.5565 % | 99.9818 % | 0.0182 % | 0.8067 % |

**Low-information keys do not produce wrong answers. They produce ambiguity.** Unique
agreement is 100 % in both buckets with zero disagreement, and recall against the reference
is 98.5–99.6 % — the correct recording is almost always *in* the block. What collapses is
uniqueness: 67.25 % / 33.18 % multi-candidate against 0.36 % / 0.81 % for normal keys.

This inverts the intuition I carried in from the block analysis. I expected a
false-positive generator; the measurement says a **cost and ambiguity** generator. Any
future rule that *suppressed* these candidates would destroy 98.5 %+ recall to fix a
problem that is not a correctness problem.

### Fallback top-20 concentration — earlier gap closed

| | |
|---|---|
| top-20 fallback blocks, candidate pairs | 569,797 |
| all fallback candidate pairs | 2,112,069 |
| **top-20 share of fallback pairs** | **26.9781 %** |
| distinct fallback keys | 131,860 |
| fallback multi-candidate listens | 217,545 |
| of those, from top-20 blocks | 40,230 (**18.4927 %**) |

Twenty keys out of 131,860 drive **27 %** of all fallback candidate pairs. Concentrated, not
diffuse.

### Decision

**Is low-information ASCII residual material, or a tail?** By listens it is a tail:
10,715 listens, **0.028 %**. By candidate pairs it is larger and concentrated: roughly
**1.4 %**, with a single key (`cvver`) responsible for 434,700 pairs.

**Concentrated or distributed?** Sharply concentrated. 82 keys in the `< 0.20` band have
cardinality ≥ 100; the remainder of the band is small.

**Do these groups agree worse with the reference?** **No.** Unique agreement is 100 % and
false-unique is 0 % in both buckets. They are worse only in *ambiguity*.

**Is there evidence to create `key_information_status` in Phase 4?** **Yes — but as a cost
and routing signal, never as a validity or suppression flag.** The evidence supports marking
these keys so scoring can skip an expensive comparison it cannot win, and so a
multi-candidate outcome here is understood as a normalization artefact rather than a genuine
tie. The evidence explicitly does **not** support dropping their candidates.

**Which cut is the candidate for evaluation in dev?** **`ascii_retention_ratio < 0.20`**,
chosen on dev-side structure only: it is where cardinality explodes (82 keys with ≥100
recordings, max 945, capturing `cvver` at 0.1957) while still covering only 0.0054 % of
listens. The holdout was used solely to check that the direction holds — it does, with the
same 100 % / 0 % agreement pattern — and was not used to select the cut.

Given the tiny listen share, the honest framing for Phase 4 is that this is a **precision-of-
diagnosis** improvement, not a recall or revenue one.

## 20. Phase 4A — cardinality baseline (SUPERSEDED, kept as the before-picture)

**This section describes an artifact that is NOT the matching result.** Phase 4A was rejected
on review with the correct diagnosis: it decided everything on candidate cardinality, with no
similarity computed anywhere, so it could not honestly call anything a tie, a confidence or a
match. Section 21 describes what replaced it.

`config/baseline_cardinality_policy.yml` (renamed from `match_tiers.yml`) is the policy;
`splitsheet_silver.baseline_cardinality_matches` is the result, preserved rather than
overwritten. What changed in the reclassification is in section 21.1.
Built by the matcher service account, which cannot read `splitsheet_eval`.

### Tiers A and B do not exist, with measured reasons

`PROJECT_SPEC.md` defines Tier A as a client-supplied `recording_mbid` and Tier B as an
exact ISRC. Both are **not implemented**, and the numbering is kept so the absence stays
visible rather than being renamed away:

- **A**: the corpus has no client-supplied MBID distinct from the ListenBrainz mapper
  output, and that output is the reference label. Building Tier A would mean feeding the
  label into matching.
- **B**: the canonical snapshot has **no ISRC column**. ISRC is on 17.4 % of listens and
  0 % of canonical rows, so the tier has no right-hand side.

### Confidence is a convention, and it was set structurally

| tier | rule | confidence | basis |
|---|---|---|---|
| C | EXACT stage, exactly one candidate | **0.95** | conservative key preserves version information; the value `PROJECT_SPEC.md` assigns |
| D | FALLBACK stage, exactly one candidate | **0.60** | the aggressive key *discarded* the information that would distinguish survivors, so one survivor is weak evidence |
| E | anything else | 0.0 | unresolved |

The 0.60 was chosen from that structural argument, not from the label. Dev-set measurement
is recorded as corroboration only, and the holdout took no part in setting it.

### Result distribution — exactly one row per listen

| tier | status | reason | listens | % |
|---|---|---|---|---|
| C | MATCHED | — | 31,421,104 | 82.2550 |
| E | UNRESOLVED | `NO_BLOCK_CANDIDATES` | 4,493,892 | 11.7642 |
| E | UNRESOLVED | `NO_LOOKUP_KEY_PARTIAL` | 1,164,629 | 3.0488 |
| E | UNRESOLVED | `NO_LOOKUP_KEY_EMPTY` | 433,180 | 1.1340 |
| D | MATCHED | — | 319,001 | 0.8351 |
| E | UNRESOLVED | `AMBIGUOUS_TIE_FALLBACK` | 217,545 | 0.5695 |
| E | UNRESOLVED | `AMBIGUOUS_TIE_EXACT` | 132,852 | 0.3478 |
| E | UNRESOLVED | `NO_ALPHANUMERIC_CONTENT` | 17,438 | 0.0456 |
| | | **total** | **38,199,641** | **100** |

The rows sum to exactly 38,199,641. `MISSING_ARTIST` and `MISSING_RECORDING` are defined
but empty, consistent with the measured 0 % null rate. **No `UNKNOWN` bucket exists.**

Black box rate: **6,459,536 listens = 16.9099 %**, every one carrying exactly one reason.

Six invariants validated before publication, all true: one row per listen; MATCHED always
carries an MBID; UNRESOLVED always carries a reason; MATCHED never carries a reason;
**no tie was ever resolved**; no `UNKNOWN`.

### Ties are never broken

350,397 listens are ambiguous (132,852 exact + 217,545 fallback) and every one has
`matched_recording_mbid = NULL`. Blocking returns candidates without ranking, so any pick
among them would be arbitrary by construction — there is no epsilon here because there is no
score to compare. `PROJECT_SPEC.md` is explicit that paying the wrong rights holder is worse
than suspending payment.

### Agreement with the mapper reference label, by tier

Correlated reference label, not ground truth. These are agreement rates, not accuracy.

| bucket | tier | evaluable | agrees | disagrees | agreement |
|---|---|---|---|---|---|
| dev | C | 24,477,158 | 24,476,224 | 934 | **99.9962 %** |
| dev | D | 11,601 | 8,937 | 2,664 | **77.0365 %** |
| dev | E | 1,238,526 | — | — | not matched, by design |
| holdout | C | 5,269,143 | 5,268,959 | 184 | **99.9965 %** |
| holdout | D | 3,268 | 2,494 | 774 | **76.3158 %** |
| holdout | E | 232,442 | — | — | not matched, by design |

The structural confidence ordering is corroborated: C and D differ by **23 percentage
points** of agreement, and dev and holdout agree to within 0.7 pp on D and 0.0003 pp on C.
Tier D at 0.60 sits below any plausible payout threshold, which is the intended consequence —
it is retained as real recall (319,001 listens) that must not be treated as confident.

Nothing here is a payout, an attribution, or a matched-revenue figure. There are no rights
data, no rate cards and no money anywhere in this project.

## 21. Phase 4B — the actual matcher: features, calibration, validation, publication

Phase 4A was a cardinality baseline. This is a scorer: 3,045,208 candidate pairs get six text
similarity features, a weighted score chosen on a calibration partition, and a decision policy
with a threshold, a margin and a tie definition that requires computed evidence.

**The ListenBrainz mapper output is a correlated reference label, not independent ground
truth. Agreement metrics must not be presented as absolute matching accuracy.**

### 21.1 What the reclassified baseline says now

`splitsheet_silver.baseline_cardinality_matches`, run `match:580a58bd52e20c60`, policy
`1.0.0+d1cf631dad36`, 38,199,641 rows. Three corrections against the rejected version:

| was | is | why |
|---|---|---|
| FALLBACK + 1 candidate → `MATCHED`, confidence 0.60 | `UNRESOLVED` / `FALLBACK_REQUIRES_SCORING` | measured 22.96 % disagreement with the reference on dev; accepting it unscored is accepting a guess |
| multi-candidate → `AMBIGUOUS_TIE_*` | `MULTIPLE_CANDIDATES_UNSCORED_EXACT` / `_FALLBACK` | calling an unranked set a tie asserts the candidates scored equally, which nothing had measured |
| `tier_confidence` 0.95 / 0.60 / 0 | **column removed from the schema** | they were neither probabilities nor computed scores. The builder now aborts if a confidence column reappears in the target table |

Distribution: 31,421,104 EXACT-unique MATCHED (82.255 %), 319,001 FALLBACK_REQUIRES_SCORING,
132,852 + 217,545 unscored multiples, 6,105,239 with no usable key or no candidate.

### 21.2 The holdout is consumed. What replaced it

> **The original holdout partition has been inspected in prior blocking and diagnostic
> analyses. It is an observed evaluation partition, not a pristine blind test set.**

It was read in the Phase 3B fallback trade-off (section 18) and again in the Phase 4 preflight
(section 19), where it confirmed the direction of a dev-side finding. It is reported from here
on as a **previously observed evaluation partition** and never again as untouched.

The old dev partition is divided in two under a **different salt**, so the division is
independent of the dev/holdout assignment rather than a recut of it
(`config/evaluation_split.yml`, `calibration_split_version` 1.0.0, frozen 2026-08-04, before
any scoring metric existed):

| partition | labelled listens | distinct reference recordings | role |
|---|---|---|---|
| calibration | 21,262,387 | 1,629,283 | features, weights, thresholds, margins |
| validation | 5,372,001 | 543,254 | opened **exactly once**, after the config was frozen |
| holdout | 6,164,815 | 543,539 | previously observed; history only |

**Recordings appearing in more than one partition: 0**, measured, not assumed. Disjointness is
by construction — the partition is a function of the recording MBID — and the unit suite proves
determinism, order-independence and that the division never touches holdout.

### 21.3 Canonical text, and why coverage is deliberately partial

Scoring compares full Unicode text, never the ASCII lookup key: the key is what discarded the
information (for `cvver`, 5 ASCII characters out of 26 Unicode alphanumerics). The listen side
already had normalized Unicode; the canonical side did not.

`splitsheet_bronze.canonical_match_texts` fills the gap using **the same Python library**, so
the normalization rules keep one implementation and are never re-expressed in SQL. Grain:
`snapshot_date + recording_mbid`, 368,795 rows — **not** the 31,554,198 of the snapshot. Only
the recordings that appear as candidates for a listen needing a score are normalized, because
normalizing the rest would be 85× the work for no consumer. `source_universe` records the
candidate run that defined the set, so widening the candidate set forces a rebuild instead of
silently scoring against stale coverage.

### 21.4 The feature table

`splitsheet_silver.silver_candidate_features`, `feat:7411e987640a1051`, 3,045,208 rows,
grain `listen_hash + candidate_recording_mbid`, built by the matcher identity.

Universe, and the cost decision inside it:

| stage | listens | pairs |
|---|---|---|
| EXACT multi-candidate | 132,852 | 933,139 |
| FALLBACK unique | 319,001 | 319,001 |
| FALLBACK multi-candidate | 217,545 | 1,793,068 |

EXACT-unique's 31,421,104 pairs are **absent on purpose**: that decision is structural and no
score participates, so computing similarity there would be 10× the work for an output nothing
reads. That is a cost decision, recorded rather than implied.

Features, all computed from both sides' normalized Unicode text:
`artist_unicode_exact`, `recording_unicode_exact`, `artist_token_similarity`,
`recording_token_similarity` (Jaccard over whitespace tokens), `artist_string_similarity`,
`recording_string_similarity` (1 − editDistance/max(len)), plus the context columns
`block_method`, `candidate_count`, `blocking_key_information_class` and
`ascii_retention_ratio`.

**Deliberately absent**: the mapper label in any form, popularity (the canonical `score`
column is never read), candidate order or row number, MBID lexical value, `artist_credit_id`,
any derived identifier, duration and ISRC (the snapshot has neither).

`blocking_key_information_class` is `POTENTIAL_LOW_INFORMATION` when the listen-side ASCII
retention ratio is below **0.20**, the cut the Phase 4 preflight measured. It is a cost and
routing signal only: it suppresses no candidate, is not a scoring input, and gets no separate
threshold.

### 21.5 Per-feature separation, on calibration only

AUC = probability a reference candidate outranks a non-reference one, ties counted as half a
win. 74,569 reference pairs against 570,207 others. Null rate 0 for all six scored features.

| feature | AUC all | EXACT_MULTI | FALLBACK_UNIQUE | FALLBACK_MULTI | NORMAL | LOW_INFO |
|---|---|---|---|---|---|---|
| recording_token_similarity | **0.9588** | 0.9760 | 0.6575 | 0.8053 | 0.9360 | 0.9865 |
| recording_string_similarity | 0.9451 | 0.9760 | 0.6619 | 0.6920 | 0.9207 | 0.9537 |
| recording_unicode_exact | 0.9350 | 0.9760 | 0.6687 | 0.6637 | 0.9210 | 0.9692 |
| release_lower_exact (probe) | 0.9278 | 0.9561 | 0.7778 | 0.7295 | 0.9025 | 0.9697 |
| artist_token_similarity | 0.8196 | 0.8896 | **0.3354** | **0.3422** | 0.7156 | 0.9598 |
| artist_string_similarity | 0.8065 | 0.8897 | **0.3307** | **0.3381** | 0.7060 | 0.9076 |
| artist_unicode_exact | 0.7935 | 0.8896 | **0.3177** | **0.3364** | 0.7005 | 0.9160 |

**Two findings I did not expect.**

1. **On both fallback stages the artist features separate in the wrong direction** (0.32–0.34).
   Higher artist similarity predicts *disagreement* with the reference there. Leave-one-out on
   the equal-weight sum confirms it: removing any artist feature *raises* overall AUC
   (0.9579 → 0.9698 without `artist_unicode_exact`), while removing any recording feature
   lowers it. An equal-weight scorer would have been measurably worse than a recording-weighted
   one, and I had no way to know that without measuring.
2. **The release probe is the third-strongest signal**, ahead of every artist feature, at a
   1.4 % null rate — and it is a *lower bound*, because it compares casefolded raw strings
   rather than normalized ones. It was included in the score on that evidence, at half weight
   for a structural reason: the canonical snapshot carries one representative release per
   recording, so a match is strong evidence and a mismatch is weak evidence.

### 21.6 Calibration: the decomposition that changed the answer

1,008 cells (6 weight sets × 7 exact thresholds × 6 fallback thresholds × 4 margins), selection
rule fixed before the table was read: **highest accepted coverage whose disagreement among
evaluable accepted decisions is ≤ 2.0 % in every scored stage**; if none qualifies, take the
lowest worst-stage disagreement and say so rather than relax the bound.

The first pass reported 6.5 % disagreement on EXACT_MULTI at every threshold, and tightening
the margin made it **worse** (7.27 % at margin 0.20, for 25 % less coverage). That is not how a
margin behaves if the score carries information, which is what prompted decomposing the
"disagreement" by cause:

| class | meaning | owner |
|---|---|---|
| `RANKING_ERROR` | reference was in the block, the score chose another | the scorer |
| `REF_NOT_RETRIEVED` | reference is in the snapshot, blocking never proposed it | blocking recall |
| `REF_NOT_IN_SNAPSHOT` | reference absent from the canonical snapshot | not evaluable |

On EXACT_MULTI, **4,379 of 67,104 accepted listens fell in the third class** — the mapper named
a recording our 2026-07-17 snapshot does not contain. Excluding those (the treatment Phase 3B
already established for `LABEL_NOT_IN_CANONICAL_SNAPSHOT`), the picture inverts:

| stage | accepted | evaluable | disagreement | ranking errors |
|---|---|---|---|---|
| EXACT_MULTI | 67,104 | 62,725 | **0.0080 %** | 4 |
| FALLBACK_UNIQUE | 3,447 | 3,115 | 2.3756 % | 0 (one candidate: none possible) |
| FALLBACK_MULTI | 1,299 | 1,126 | 3.1083 % | 19 |

The apparent 6.5 % was a blocking-recall and label-universe artifact; the scoring error on
exact blocks is **4 listens in 62,725**. A first pass that had not been decomposed would have
rejected a working ranker.

The selection rule also had to be fixed before it could be trusted: a stage that accepts
nothing scores 0/0 disagreement, so the first run's "winner" was a configuration that accepted
zero fallback listens. Requiring ≥ 1,000 evaluable accepted listens per stage closes that
degeneracy. **That is arithmetic, not a relaxed bound** — the 2.0 % ceiling never moved, and
no cell met it, which is recorded in the config itself.

Frozen: `config/scoring_rules.yml` **1.0.0+cb21f9704ff0**. Weights 0.1 / 1.0 / 0.1 / 1.0 /
0.1 / 1.0 / 0.5 (artist / recording × exact, token, string, then release), exact threshold
0.55, fallback threshold 0.85, minimum margin 0.02, tie epsilon 0.01. Segment checks at the
chosen cell: `POTENTIAL_LOW_INFORMATION` disagreement 0.0000 % on 403 evaluable exact-multi
acceptances against 0.0080 % for NORMAL, and NON_LATIN 0.0457 % against LATIN 0.0066 %.

### 21.7 Validation, opened exactly once

Run under `1.0.0+cb21f9704ff0` against the **published** table, not a recomputation. The
loader refuses to report if the config on disk no longer matches the version that produced the
rows.

| partition | labelled listens | coverage | evaluable accepted | disagreement | ranking errors |
|---|---|---|---|---|---|
| **validation** (blind, once) | 5,372,001 | 96.0119 % | 4,989,788 | **0.0066 %** | 24 |
| calibration (tuned on) | 21,262,387 | 95.1911 % | 19,574,628 | 0.0040 % | 23 |
| holdout (previously observed) | 6,164,815 | 96.5505 % | 5,311,847 | 0.0048 % | 3 |

Per method, on the validation partition:

| match_method | listens | matched | evaluable | agreement | disagreement |
|---|---|---|---|---|---|
| STRUCTURAL_EXACT_UNIQUE | 5,135,873 | 5,135,873 | 4,969,496 | 99.9945 % | 0.0055 % |
| SCORED_EXACT_MULTIPLE | 20,547 | 20,453 | 19,068 | 99.8899 % | 0.1101 % |
| SCORED_FALLBACK_UNIQUE | 3,615 | 893 | 809 | 96.7862 % | **3.2138 %** |
| SCORED_FALLBACK_MULTIPLE | 3,587 | 542 | 415 | 97.8313 % | 2.1687 % |

**The generalisation gap is real and is not smoothed over.** Exact-multi degrades from 0.008 %
on calibration to 0.1101 % on validation — a factor of 14, on 21 error listens. Fallback-unique
goes from 2.38 % to 3.2138 %, above the 2 % bound the calibration rule had already failed to
meet; the previously observed holdout puts the same stage at 4.9513 %. So the honest statement
is: **accepting a fallback candidate on text similarity alone carries a 2–5 % disagreement
rate with the reference label, and no threshold in the grid removed that.**

Per the freeze discipline, **nothing was retuned after seeing this**. Changing a weight or a
threshold now requires a new `scoring_version` and a new validation strategy or new data; the
consumed partition cannot be reused to justify a changed rule.

### 21.8 The published result

`splitsheet_silver.silver_listen_matches`, run `match:101eef5c5b5c081e`, scoring
`1.0.0+cb21f9704ff0`, 38,199,641 rows, 18 columns (the 17 required plus `listened_at`, which
the table is partitioned on). `tier_confidence` is gone.

| | listens | share |
|---|---|---|
| **MATCHED** | **31,598,529** | **82.7194 %** |
| — STRUCTURAL_EXACT_UNIQUE (no score computed) | 31,421,104 | 82.2550 % |
| — SCORED_EXACT_MULTIPLE | 130,886 | 0.3426 % |
| — SCORED_FALLBACK_UNIQUE | 35,007 | 0.0917 % |
| — SCORED_FALLBACK_MULTIPLE | 11,532 | 0.0302 % |
| UNRESOLVED / NO_BLOCK_CANDIDATES | 4,493,892 | 11.7642 % |
| UNRESOLVED / NO_LOOKUP_KEY_PARTIAL | 1,164,629 | 3.0488 % |
| UNRESOLVED / BELOW_THRESHOLD | 490,883 | 1.2851 % |
| UNRESOLVED / NO_LOOKUP_KEY_EMPTY | 433,180 | 1.1340 % |
| UNRESOLVED / NO_ALPHANUMERIC_CONTENT | 17,438 | 0.0456 % |
| UNRESOLVED / AMBIGUOUS_TIE | 1,090 | 0.0029 % |

Scoring converted **177,425 listens** that the baseline left unresolved into decisions, and
refused 490,883 more on measured evidence rather than on cardinality. Coverage 82.255 % →
82.7194 %.

Ten invariants, asserted in staging before publication and again as integration tests against
the published table: one row per listen; MATCHED has an MBID; UNRESOLVED has none; UNRESOLVED
has exactly one failure reason; MATCHED has none; `AMBIGUOUS_TIE` has ≥ 2 candidates; a tie
never carries an MBID; an accepted fallback-unique always scores above the fallback threshold;
no acceptance with several candidates has a margin below the minimum; no `UNKNOWN` bucket.
Two more that matter as much: structural acceptance carries **no** score or margin, and
fallback-unique carries **no** margin at all — which is what makes "a lone candidate can never
be a tie" visible in the data rather than only in prose.

`ANY_VALUE` is gone. The sole-candidate CTE keeps only groups of exactly one row, so `MIN` over
that grain returns that row: uniqueness holds **by construction** and is also validated,
instead of being an arbitrary pick that a later assertion happened to bless.

Two implementations of the same rules are unavoidable here — Python for tests, SQL for 3M rows
— so they are generated from one definition **and compared on real data**: integration tests
recompute published features and published scores in Python from the same inputs and assert
agreement, and replay the decision policy over published top-1/top-2 to confirm every published
outcome is what `decide()` returns.

### 21.9 What is still unmatched, by cause

| failure_reason | listens | % of all | top-20 concentration |
|---|---|---|---|
| NO_BLOCK_CANDIDATES | 4,493,892 | 11.7642 % | 6.10 % |
| NO_LOOKUP_KEY_PARTIAL | 1,164,629 | 3.0488 % | 35.34 % |
| BELOW_THRESHOLD | 490,883 | 1.2851 % | 17.91 % |
| NO_LOOKUP_KEY_EMPTY | 433,180 | 1.1340 % | 2.69 % |
| NO_ALPHANUMERIC_CONTENT | 17,438 | 0.0456 % | 24.96 % |
| AMBIGUOUS_TIE | 1,090 | 0.0029 % | 36.79 % |

The shape of the remaining work is legible in those numbers, and the concentration is where
the next unit of work is.

**`NO_LOOKUP_KEY_PARTIAL` is the most concentrated failure in the pipeline: 35.34 % of it comes
from 20 combinations, and a single one — `Agust D` / `해금` — is 384,926 listens, 33.05 % of the
whole reason and 1.01 % of the entire corpus.** The pattern is a Latin artist with a non-Latin
title: the artist half of the key survives, the title half does not, and the combined key is
never emitted because emitting half a key would collide every unromanisable title by that
artist into one bucket. That rule is correct and is not being relaxed here — but it means one
transliteration path for Korean and Japanese titles would recover more listens than any scoring
change now available.

`BELOW_THRESHOLD` is next: 20 combinations account for 17.91 % of it, led by titles carrying
version markers the fallback rules do not strip (`NORMAL (Explicit Ver.)` 18,186 listens,
`NORMAL (Clean Ver.)` 4,212) and collaboration credits the artist comparison splits
(`Closer (with Paul Blanco, Mahalia)`, `NEURON (with Gaeko & YOON MIRAE)`). Those are
normalization gaps presenting as scoring failures.

`NO_LOOKUP_KEY_EMPTY` and `NO_BLOCK_CANDIDATES` are dominated by non-Latin content
(Japanese, Korean, Cyrillic), which is the same finding as section 19 seen from the other end.
`NO_ALPHANUMERIC_CONTENT` is genuinely untitled content: `??????`, `( ╥﹏╥ )`, `&`, `.`, `💰`,
Morse code. No amount of matching fixes a track whose title is an emoji.

The output carries only submitted artist and recording strings plus counts. **No `user_id`, no
`recording_msid`, no `listen_hash`** — asserted in the script, not assumed.

## 22. Phase 5A — modeled rights, temporal validity, and the dbt foundation

Everything in this section rests on a declaration that has to come first.

| what | status |
|---|---|
| ListenBrainz listens, MusicBrainz recordings | **REAL** |
| rights holders, ownership splits, rate cards | **MODELED**, generated from a versioned seed |
| any royalty amount | **illustrative modeled amount, never an observed industry payout** |

No real company, label, publisher, writer or performer appears anywhere. Holder display names are
`Modeled Rights Holder NNNNNN` **by construction**, so no generated string can coincide with a real
organisation, and every generated row carries `is_modeled = TRUE` so the declaration travels with
the data rather than living only in documentation.

**No payout amount is computed anywhere in this phase.** Streams, share and rate are brought
together and classified; they are deliberately not multiplied.

### 22.1 The Phase 4B freeze, enforced by tests

`config/frozen_versions.yml` records the versions that produced the published result, and
`tests/unit/test_frozen.py` fails if the live configs stop matching it. A change to a normalization
rule, a scoring weight or a threshold is therefore not an edit — it breaks the suite, which is the
point. The manifest also asserts internal consistency: the frozen per-method counts must sum to
31,598,529 matched and 38,199,641 total, so a typo in the freeze is caught too.

Transliteration of Korean and Japanese titles is registered in `docs/restatement_candidates.md`
with the evidence preserved at freeze time — 1,164,629 `NO_LOOKUP_KEY_PARTIAL` listens, of which
**`Agust D` / `해금` alone is 384,926 (33.05% of the reason, 1.01% of the corpus)** — and
deliberately not implemented.

### 22.2 Sizing before generating, and a hypothesis I rejected

Measured on the frozen result before any rights data existed:

| | listens | distinct recordings |
|---|---|---|
| total | 38,199,641 | — |
| matched (technical) | 31,598,529 | 2,471,846 |
| **payout-eligible** under the policy below | **31,563,522** (82.6278%) | 2,465,963 |
| **risk-held** (`MATCH_RISK_POLICY`) | **35,007** (0.0916%) | 11,673 |
| unmatched | 6,601,112 | — |

5,790 recordings appear in **both** the eligible and risk-held groups, which is why rights are
generated for every matched recording rather than for the eligible subset: a recording held today
becomes eligible under a future policy with no regeneration. The temporal join runs at
**8,889,078 eligible recording-days**, not at 31.6M listens — same answer, 3.5× less work.

The first sizing pass assumed ~3 recordings per holder and estimated **1,647,897 rights holders**
for 2,471,846 recordings. That models a world where nearly every recording has its own publisher,
which is wrong, so the holder count was set explicitly to 60,000 with a documented skew formula and
the estimate redone. The corrected estimate predicted **4,740,012** ownership rows; the generator
produced **4,741,031** — 0.02% out, which is the sizing step doing its job.

### 22.3 The generator: deterministic, versioned, and defective on purpose

`config/rights_model.yml` (`1.0.0+47f801102e17`, digest-sealed) is the contract;
`src/rights/generator.py` is a pure function of `sha256(seed | kind | key)`. No RNG object, no
wall-clock, no read order — so ownership for any recording can be generated independently and in
any order, and a re-run reproduces the same rows and the same ids. Six mutation tests prove that
changing the seed, holder count, a defect quota, an interval or a rate without updating the digest
stops the generator.

Landed: **60,000 holders, 4,741,031 ownership rows** over 2,472,146 recordings (2,471,846 real +
300 deliberately orphaned), **2 rate card rows**, `generation_run_id rights:d2b8a2dc146673e9`.

Shares are `NUMERIC(9,4)` and rates `NUMERIC(18,9)`. Never `FLOAT`: a share is money's denominator,
and a sum of four floats is not exactly 100. Healthy sets sum to **exactly** `100.0000`, measured
on 2.5M sets with min = max = 100.0000.

**Deliberate defects, and the honest bit about them: the source does not label them.** There is no
`defect_class` column anywhere. If the source announced its own defects, "detecting" them
downstream would be a lookup rather than a check. The injected counts live in
`docs/phase0/rights_generation.json` and the quality layer has to find them independently:

| defect | injected | detected by dbt | agrees |
|---|---|---|---|
| shares do not sum to 100 | 500 | 500 | yes |
| temporal overlap | 400 | 400 | yes |
| temporal gap | 400 | 400 | yes |
| invalid interval (`valid_to <= valid_from`) | 200 | 200 | yes |
| missing rights holder | 250 | 250 | yes |
| orphan recording MBID | 300 | 300 | yes |
| holder with no ownership row | 1,000 | 1,000 | yes |

Every temporal defect is placed **inside 2026-06**, so it is visible in the period the project
actually joins rather than in a synthetic corner of the calendar.

### 22.4 Temporal ownership modeling with validity intervals — NOT SCD Type 2

`ownership_splits` uses half-open intervals `[valid_from, valid_to)` with `9999-12-31` as the
open-ended sentinel — an explicit date rather than `NULL`, because a `NULL` upper bound in a
half-open predicate silently becomes "always true".

This is **not** called SCD Type 2, and the distinction is not pedantry: nothing here detects a
change and closes a row. The intervals come straight out of the generator. `split_version_id`
identifies one split *set* — a recording plus the date its ownership took effect — so a healthy
recording-date resolves to exactly one, and an ownership change produces a **new** set rather than
a mutated one.

**The join, everywhere:** `listen_date >= valid_from AND listen_date < valid_to`.

Proven at all three boundary positions on real rows, and proven to matter: for **200 of 200**
probed recordings with a 2026-06-15 ownership change, the holders selected on 06-14 differ from
those selected on 06-15, with **0** unchanged. The holder sets either side of a change are disjoint
by construction, so a wrong predicate produces a visibly wrong holder rather than a slightly wrong
share.

### 22.5 SCD Type 2, demonstrated for real

`dbt snapshot` over `rights_holders`, strategy `check` on the tracked attributes. `timestamp`
strategy was rejected because the generated source has no reliable last-modified column and
inventing one would make the snapshot detect wall-clock noise instead of content changes.

The controlled proof: snapshot, then re-land **only** `rights_holders` with 250 `payee_status`
values flipped (`build_rights.py --revise-payee-status 250`,
`generation_run_id rights:085bed00fc2cb51e`), then snapshot again. The second run reported
`MERGE (500.0 rows)` — 250 closed plus 250 inserted:

| property | measured |
|---|---|
| versions / holders | 60,250 / 60,000 |
| current versions | 60,000 — exactly one per holder |
| closed versions | 250 |
| holders with history | 250 — exactly the number revised |
| old values still readable | yes: e.g. `MRH-000000` `ACTIVE` (closed) → `PENDING_VERIFICATION` (current) |

Nothing was overwritten. `dim_rights_holders` exposes both versions with `is_current`.

### 22.6 Payout eligibility: MATCHED != PAYABLE

The layer exists because a technical match is not an authorisation to pay.

| | listens |
|---|---|
| payout-eligible | 31,563,522 |
| held — `NOT_MATCHED_NO_BLOCK_CANDIDATES` | 4,493,892 |
| held — `NOT_MATCHED_NO_LOOKUP_KEY_PARTIAL` | 1,164,629 |
| held — `NOT_MATCHED_BELOW_THRESHOLD` | 490,883 |
| held — `NOT_MATCHED_NO_LOOKUP_KEY_EMPTY` | 433,180 |
| **held — `MATCH_RISK_POLICY`** | **35,007** |
| held — `NOT_MATCHED_NO_ALPHANUMERIC_CONTENT` | 17,438 |
| held — `NOT_MATCHED_AMBIGUOUS_TIE` | 1,090 |

The matcher was **not** modified, no new threshold was chosen, and the policy is **not** presented
as validated by the consumed validation partition. The partition measured the matcher; the decision
to hold rather than pay is a risk judgement made outside it, and it is reversible by editing one
model.

**A test of mine was wrong here, and the correction is instructive.** `match_method` describes how
a listen was *evaluated*, not what the matcher concluded: `SCORED_FALLBACK_UNIQUE` covers 319,001
listens, of which the matcher **accepted 35,007** and **refused 283,994** as `BELOW_THRESHOLD`. My
first assertion demanded `MATCH_RISK_POLICY` for all of them and failed on 283,994 rows. The model
was right — a refused listen is not matched at all and is held for the ordinary reason.

### 22.7 The temporal join at real scale

8,889,078 eligible recording-days:

| resolution_status | recording-days | streams | attributable |
|---|---|---|---|
| `RESOLVED` | 7,953,823 | 28,386,887 | 7,953,823 |
| `RATE_CARD_GAP` | 929,346 | 3,164,839 | 0 |
| `DEFECTIVE_OWNERSHIP` | 5,909 | 11,796 | 0 |
| `MULTIPLE_VALID_SETS` | 0 | — | 0 |

`RATE_CARD_GAP` is the deliberate three-day hole (2026-06-10 to 06-12): 929,346 recording-days
carrying 3,164,839 streams are **held, not priced at zero and not dropped**. A pipeline that
interpolates a rate for an uncovered day is inventing money.

`MULTIPLE_VALID_SETS` is zero, and that is the design working rather than the check failing:
overlapping sets are marked invalid upstream, so they never both qualify as valid. The
classification exists so that if a future generation produces two *valid* sets covering one day, the
row is refused instead of silently resolved. Uniqueness holds **by construction** — the
`sole_valid_set` CTE keeps only groups of exactly one covering set, so the `MIN` inside it returns
that row rather than picking from a set.

One reading note: 5,296 `DEFECTIVE_OWNERSHIP` recording-days carry a `rate_per_stream`. That is
correct — a rate is a property of the day, not of the ownership — and `is_attributable` is 0 for all
of them, because an amount cannot be computed from half of a contract.

### 22.8 Data quality as data

`quality_report` is a table, not a log: one row per rule per run with `run_id` (dbt's invocation
id), `model`, `rule`, `severity`, `status`, `failed_records`, `total_records`, `failure_rate`,
`business_impact` and an `expectation` column separating "must be zero" from "expected to find the
injected defects". 17 rules; the MODELED source contains deliberate defects, so a rule reporting
zero on those would mean the check is broken.

| business impact | example rule | failed |
|---|---|---|
| `wrong_rights_holder_risk` | `no_temporal_overlap` | 800 sets |
| `wrong_rights_holder_risk` | `rights_holder_exists` | 250 sets |
| `ownership_allocation_at_risk` | `valid_set_shares_sum_to_exactly_100` | 500 sets |
| `royalty_attribution_at_risk` | `rate_card_covers_the_day` | 929,959 recording-days |
| `royalty_attribution_at_risk` | `eligible_recording_day_resolves_to_an_owner` | 935,255 recording-days |
| `royalty_attribution_at_risk` | `holder_has_at_least_one_split` (WARN) | 1,000 holders |

Six rules that must be zero — share range, one interval per split set, no double-covered rate day,
eligible listens carrying a recording, held listens carrying a reason — **are** zero.

### 22.9 The warehouse boundary

Terraform owns the datasets; dbt owns their contents. `splitsheet_rights` holds the Terraform-managed
source tables with declared `NUMERIC` precision and scale, and `splitsheet_dbt` holds models,
snapshots and the quality report created by dbt. `terraform plan` therefore stays at **No changes**
while dbt rebuilds freely, and the financial joins live in reviewable SQL rather than inside Python.

`dbt build`: **64 passed, 0 errors** (1 snapshot, 6 tables, 4 views, 53 data tests).
`dbt parse` runs with **no credentials at all**, so public CI validates every ref, source, macro and
YAML contract without touching GCP.
