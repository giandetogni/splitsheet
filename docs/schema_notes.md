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
> - Not started: blocking, matching, silver/gold, dbt, rights data, Airflow, Dataproc.

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

This column is a **ListenBrainz mapper reference label**. It is *not* ground truth and
must never be described as such, in this repository or anywhere else.

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
  `label_coverage`, `precision_vs_mapper`, `recall_vs_mapper`, `false_positive_count`,
  `abstention_rate`, `matcher_vs_mapper_divergences`, and results for the
  **unlabelled subset reported separately** with no accuracy claim attached.

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

**Staged beats blanket, confirmed with production code:**

| | % of all listens |
|---|---|
| exact unique | 71.1931 |
| + unique gained by staged fallback | 3.2025 |
| **= staged payable** | **74.3956** |
| blanket aggressive payable | 49.8402 |
| **staged advantage** | **24.56 pp** |

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
