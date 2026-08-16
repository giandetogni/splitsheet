# Cost

## Phase 0 — measured, $0

No GCP resource was created, so Phase 0 cost nothing. What it did consume was network
transfer, which was accounted for byte by byte:

| item | bytes | notes |
|---|---|---|
| ListenBrainz incremental dump 2611 | 188,379,424 | schema/identifier profiling |
| MusicBrainz canonical dump | 2,320,377,487 | 31.5 M recordings |
| ListenBrainz sample dump | 246,734,240 | checked for listens; contains none |
| Full-export indexing + period location | 117,190,964 | **0.0571 %** of the 191 GiB archive |
| Preserved 2026-06 slice | 2,671,810,509 | 23 members, 0 retries |

The indexing figure is the one worth keeping: locating and sizing a month inside a
205,073,162,240-byte archive cost 117 MB and 1,744 requests, because the tar is
uncompressed and the server honours Range requests.

## Phase 1A — estimated before upload, then measured

Slice: **2,671,810,509 bytes = 2.4883 GiB**, 23 objects plus one manifest.

Region **us-central1**, class **STANDARD**, single-region.

| line item | basis | estimate |
|---|---|---|
| storage | 2.4883 GiB × $0.020/GiB-month | **$0.0498 / month** |
| upload (ingress) | free | $0.00 |
| Class A operations | 24 writes × $0.005/1,000 | $0.00012 |
| verification egress | ~6 MB × $0.12/GB | $0.0007 |
| **first month total** | | **≈ $0.05** |
| **annual, steady state** | | **≈ $0.60** |

### Storage class — why STANDARD

| class | $/GiB-mo | this slice | trade |
|---|---|---|---|
| **STANDARD** | 0.020 | **$0.0498/mo** | no retrieval fee, no minimum duration |
| NEARLINE | 0.010 | $0.0249/mo | 30-day minimum, per-GB retrieval fee |
| COLDLINE | 0.004 | $0.0100/mo | 90-day minimum, higher retrieval fee |
| ARCHIVE | 0.0012 | $0.0030/mo | 365-day minimum, highest retrieval fee |

Choosing STANDARD costs **2.5 cents a month** more than NEARLINE. In exchange it removes
early-deletion penalties and per-read retrieval charges from the one asset in this project
that cannot be re-downloaded once upstream rotates the artifact. At this data size the
cheaper classes optimise a rounding error while adding failure modes, so the choice is
STANDARD until the slice is no longer the only copy of anything.

### Region — why us-central1, single region

Single-region rather than multi-region because the slice is read by batch jobs that will
run in the same region; multi-region would pay for geo-replication that nothing needs.
`us-central1` because it is the cheapest tier, it is where the later BigQuery dataset will
live (load jobs require compatible locations), and it matches the target market for this
work. Recorded here because region is effectively immutable: changing it later means
recreating the bucket and re-uploading.

### Estimate limitations — read before quoting these numbers

- Rates above are **list prices applied from memory, not fetched from the pricing API**.
  They must be confirmed against the current Cloud Storage pricing page before being
  presented as fact anywhere public.
- Excludes any free-tier allowance, which may make the real bill $0.
- Excludes cost of the later BigQuery, Dataproc and Composer work, none of which exists.
- Object versioning is enabled. It adds nothing while objects are written once, but a
  re-upload would store a second version and roughly double storage for that object.
- **A budget alert is not a spending cap.** See the Phase 1B section below: one now
  exists, and it still cannot stop spend.

### Measured after execution

| | estimated | actual |
|---|---|---|
| bytes uploaded | 2,671,810,509 | **2,671,810,509** (0 retries, 0 failures) |
| upload duration | — | **198.6 s** (~13.5 MB/s) |
| objects | 23 + manifest | **23 + manifest** |
| stored bytes | 2,671,810,509 | **2,671,816,509** (manifest adds 6,000 B) |
| verification egress | ~6 MB | **2,891,884 B** |
| storage / month | $0.0498 | unchanged: same GiB, same class, same region |

Verifying 2.67 GB of cloud data cost 2.89 MB of egress, because the check reads Parquet
footers by ranged GET and the `listened_at` column only for the two boundary members.
Bucket created in `US-CENTRAL1`, class `STANDARD`, as planned.

## Teardown

`terraform destroy` **fails** on the raw bucket by design: `prevent_destroy = true`
blocks destruction through Terraform, and `force_destroy = false` stops the provider
from emptying the bucket on its way to deleting it. Removing it through Terraform is
therefore a two-step, deliberate act: delete the `prevent_destroy` block in a reviewed
commit, then destroy.

This is friction, not immutability. Anyone with sufficient IAM permissions can delete the
objects directly and then remove the now-empty bucket, entirely outside Terraform.
Versioning and soft delete extend the window to recover from that; they do not prevent
it. Real prevention would need a locked retention policy, which is irreversible and was
deliberately declined (see `terraform/raw-storage/main.tf`).

## Phase 1B — budget alert

`billingAccounts/01D939-A7BDBF-1BD0A5/budgets/06f7cf90-d916-4cbb-9cbc-086fc3daa0c2`,
managed by `terraform/governance`.

| | |
|---|---|
| amount | **BRL 50 / month** (billing account currency verified as BRL) |
| scope | **`projects/227919566032` only** (= `ss-de-944054e7`) |
| period | `MONTH` |
| credits | `INCLUDE_ALL_CREDITS` |
| thresholds | **50 %, 80 %, 100 %**, all on `CURRENT_SPEND` |
| recipients | default billing-account IAM administrators and users |

Verified with `gcloud billing budgets describe`, not from Terraform output.

### A budget is not a spending cap

This must not be misread as a control. A Cloud Billing budget **only sends
notifications**. It cannot stop a query, cancel a job, delete a resource or disable
billing. If something runs away — a BigQuery scan without a partition filter, a Dataproc
cluster left running — spend will exceed BRL 50 and the budget will do nothing except
tell someone afterwards.

R$50 is roughly 100× the measured steady-state cost (~R$0.30/month for 2.49 GiB of
STANDARD storage). It is set that high deliberately: a threshold near the true baseline
would fire on rounding noise and be ignored, and an alert people ignore is worse than no
alert. Crossing even 50 % of this budget means something materially changed.

Real spend control needs different mechanisms, none of which exist yet: partition-filter
requirements and `maximum_bytes_billed` on BigQuery, teardown of ephemeral compute, and
quota limits. Those belong to the phases that introduce that spend.

## Phase 2A — BigQuery ingestion

Load jobs are free; every cost below is query/DML bytes billed.

| step | bytes billed | slot ms |
|---|---|---|
| timestamp probe (1 file) | 13,631,488 | 2,089 |
| reconciliation | 0 | 0 |
| collision analysis | 6,695,157,760 | 928,234 |
| column coverage | 0 | 0 |
| bronze insert (manual) | 4,192,206,848 | 1,417,264 |
| labels insert (manual) | 3,396,337,664 | 686,384 |
| loader run A (insert) | 10,454,302,720 | 2,061,248 |
| loader run B (skip) | 4,928,307,200 | 147,378 |
| **total** | **29,679,943,680** | **5,242,597** |

29,679,943,680 bytes = **0.0270 TiB**. At the on-demand list rate of $6.25/TiB that is **≈ $0.17**, and it falls inside the 1 TiB/month free query tier, so the expected invoice line is **$0.00**.

Storage added: `bronze_listens` and `mapper_reference_labels` are materialised copies of ~38.2 M rows; the external table stores nothing. Both sit well inside the 10 GiB free storage tier at this size.

Two cost facts worth carrying forward:

- **Dry runs are useless against external tables.** Every dry run in this phase reported `totalBytesProcessed: 0` with `LOWER_BOUND` accuracy, because BigQuery cannot estimate GCS bytes ahead of time. Estimation only becomes meaningful once queries hit materialised tables, which is one concrete argument for materialising bronze rather than querying the external table repeatedly.
- **`require_partition_filter` is doing real work.** An unfiltered `SELECT COUNT(*)` on `bronze_listens` is rejected outright, so the accidental full-table scan cannot happen.

Rates are list prices applied from memory, not fetched from the pricing API.

## Phase 2B — contract, failure-safe publication, firewall

17 jobs, **42,010,148,864 bytes billed = 0.0382 TiB**, 4,296,391 slot-ms.
List-price equivalent **≈ $0.24**, a conversion of consumption rather than a known charge.
Consumption sits inside the 1 TiB monthly free query allowance, so the actual monetary cost
is **UNKNOWN without billing evidence** and is expected to be $0.00.

| run | jobs | bytes billed | slot ms |
|---|---|---|---|
| publish (stage → validate → atomic swap) | 10 | 37,060,870,144 | 4,136,309 |
| re-run (skipped, no write) | 3 | 4,928,307,200 | 159,880 |
| orphan staging cleanup | 4 | 20,971,520 | 202 |

The re-run costs 4.93 GB because the pre-check still reconciles the source before deciding
to skip. That is deliberate: a cheap skip that trusts the source without re-reading it
would not notice the slice changing underneath. The alternative — skipping on the strength
of the target alone — saves about 5 GB per run and gives up the contract check, which is
the wrong trade at this price.

Staging roughly doubles peak storage during a publish (two 38.2 M-row tables exist at
once) and drops back after commit. At this size both remain inside the free storage tier.

## Phase 2C — CI

The public workflow costs nothing: it has no credentials and no path to GCP.

The read-only integration workflow, measured from
`INFORMATION_SCHEMA.JOBS_BY_PROJECT` filtered to the CI identity:
**14 jobs, 10,671,357,952 bytes billed = 0.0097 TiB, 403,418 slot-ms.**
At the $6.25/TiB list rate that is **≈ $0.06 per manual run**, inside the free tier.

Most of that is the reconciliation test recomputing 39.2 M rows from the external table.
Every query in the suite is capped by `maximum_bytes_billed = 50 GiB`, so a mistake in a
test cannot turn into a large bill.

Because the workflow is `workflow_dispatch` only, this cost is incurred deliberately and
never by a push or a pull request — and never by a fork.

## Phase 3B — canonical ingestion and staged blocking

Measured from `INFORMATION_SCHEMA.JOBS_BY_PROJECT` over the phase window:
**224 query jobs, 584,590,557,184 bytes billed = 0.5317 TiB, 58,720,559 slot-ms.**
**List-price equivalent ≈ $3.32.** That is a conversion of processing consumption at the
$6.25/TiB on-demand rate, **not** an amount known to have been charged. The actual monetary
cost is **UNKNOWN** until the billing account is inspected, and is plausibly $0: the
measured consumption is about 53 % of the 1 TiB monthly free query allowance.

Largest single contributors:

| step | bytes billed |
|---|---|
| load `bronze_canonical_recordings` (31.5 M rows) | 7,519,338,496 |
| staged blocking build, end to end | 42,752,540,672 |
| candidate-space and evaluation queries | tens of GB each over 34–58 M row joins |

Storage added: canonical CSV 1.99 GB and the blocking index 1.20 GB in GCS, plus
`bronze_canonical_recordings` (31.5 M rows), `canonical_blocking_index` (58.8 M rows),
`listen_pair_normalization` (4.6 M rows), `silver_listens_normalized` (38.2 M rows) and
`silver_match_candidates` (34.5 M rows) in BigQuery. This is the point at which storage
stops being free-tier noise and starts being a real line item; it should be watched in the
next phase rather than assumed.

Two cost lessons worth carrying:

- **Normalizing per distinct pair instead of per listen** cut the Python work 8.30×
  (4,599,791 instead of 38,199,641) and moved zero rules into SQL.
- **Iterating on a 31.5 M-row external table is expensive.** Several of those 224 jobs were
  re-reads caused by my own diagnosis errors (`compression: NONE`, an ambiguous column, a
  schema overwritten by `bq load --replace`). Getting the table definition right on paper
  before scanning would have saved a measurable fraction of the 0.53 TiB.


## A note on every figure in this document

Every currency figure here is a **list-price equivalent**: bytes billed converted at the
published on-demand rate. It is a measure of **processing consumption**, not of money known
to have left the account.

No billing export has been inspected, so the **actual monetary cost of this project is
UNKNOWN**. Given that consumption has stayed within the free query allowance every month so
far, the real invoice may well be $0.00 — but that is an expectation, not evidence, and this
document will not claim otherwise until a billing export says so.

## Phase 4 preflight — ASCII residual analysis

Read-only, 6 query jobs. **47,130,812,005 bytes processed, 47,133,491,200 billed
= 0.0429 TiB**, 4,373,301 slot-ms, 54,973 ms wall.

Dry-run estimates were reliable here because every query reads materialised tables rather
than an external table: 6,660,108,241 / 11,654,921,446 / 12,079,772,161 / 13,178,229,491
bytes against actuals in the same range. Every query ran under `maximum_bytes_billed`.

**List-price equivalent ≈ $0.27.** A conversion of consumption, not a known charge.
**Actual monetary cost UNKNOWN without billing evidence.**

## Phase 4B — features, calibration, validation, publication

Eight scripted stages, 41 query jobs plus one load job.

| stage | bytes billed | slot-ms |
|---|---|---|
| baseline reclassification and republication | 22,139,633,664 | 1,860,403 |
| canonical match texts (query + load + insert) | 8,144,289,792 | — |
| candidate feature table (3,045,208 pairs) | 12,382,633,984 | 1,149,858 |
| per-feature analysis on calibration | 144,589,193,216 | 5,867,725 |
| threshold/weight grid (1,008 cells, run twice) | 28,893,511,680 | 1,684,439 |
| scored match results | 30,855,397,376 | 2,948,011 |
| validation (three partitions × three cuts) | 130,572,877,824 | 9,399,873 |
| top-20 unmatched | 11,220,811,776 | — |
| **total** | **388,798,349,312 = 0.3536 TiB** | **22,910,309** |

**List-price equivalent ≈ $2.21.** A conversion of processing consumption at the published
on-demand rate, **not** a known charge. **Actual monetary cost remains UNKNOWN without billing
evidence.**

Two observations worth carrying forward:

- **The evaluation queries cost 4× the pipeline they evaluate.** Feature analysis and validation
  together are 275 GB of the 389 GB, because each of the 34 AUC and metric queries re-reads the
  label table joined to the candidate table. Materialising a single joined evaluation table once
  would cut that by roughly two thirds. It was not done because the analysis ran once.
- **One avoidable cost was incurred**: the calibration grid ran twice (once before the error
  decomposition existed, once after), and the second run of the extended grid re-read the same
  8.2 GB input three times for the chosen-cell breakdowns. Roughly 20 GB, about $0.12
  list-price equivalent, spent learning that the first selection rule had a 0/0 degeneracy.

Free-tier context: the monthly on-demand query allowance is 1 TiB. This phase consumed
**0.3536 TiB** of it. Cumulative project consumption is now roughly 0.94 TiB across all phases,
so the free allowance has probably absorbed all of it — **probably, on arithmetic, not on
billing evidence**.

## Phase 5A — modeled rights, temporal validity, dbt

| stage | bytes billed |
|---|---|
| sizing (read-only, 4 queries) | 11,056,185,344 |
| generation: universe query + 3 publish transactions + verify | ~2,800,000,000 |
| controlled revision (holders only) | ~10,000,000 |
| dbt build (snapshot, 6 tables, 4 views, 53 tests, twice) | ~3,900,000,000 |
| verification and reconciliation (8 queries) | 4,040,163,328 |
| **total** | **≈ 18,799,919,104 = 0.0171 TiB** |

**List-price equivalent ≈ $0.11.** Processing consumption converted at the published on-demand
rate, **not** money known to have been charged. **Actual monetary cost remains UNKNOWN without
billing evidence.**

Three things worth carrying forward:

- **The sizing step paid for itself.** It cost 11 GB of the 18.8 GB total — the single largest line
  — and it caught a holder-count assumption that was wrong by a factor of 27 (1,647,897 estimated
  against 60,000 chosen) before any row was generated. It also predicted the ownership row count to
  within 0.02% (4,740,012 estimated, 4,741,031 actual).
- **Aggregating before joining is what kept this cheap.** The temporal ownership join runs at
  8,889,078 eligible recording-days rather than 31,563,522 listens. Same answer, 3.5× less input.
- **dbt is cheap here because the models are tables, not views.** `int_ownership_validity` and
  `int_ownership_resolution` are referenced by the quality report and by six tests; as views, each
  reference would re-scan 4.7M ownership rows and 8.9M recording-days.

Cumulative project consumption is now roughly **0.96 TiB** across all phases. The monthly on-demand
allowance is 1 TiB, so the free tier has probably absorbed all of it — **probably, on arithmetic,
not on billing evidence**.

## Phase 5B — payout policy, attribution, publication

| stage | bytes |
|---|---|
| dry-run estimate for `int_financial_disposition`, recorded before materialising | 6,933,335,858 *(estimate)* |
| publisher queries: input verification, digests, pointer, reconciliation, money summary (13 jobs) | 8,299,479,040 *(billed)* |
| dbt materialisations: disposition 38.2M rows, attributable streams 3.2M, fact 5.9M, plus 63 tests, run three times across build, idempotency proof and rehearsal | ~14,000,000,000 *(billed, from dbt's own job logs)* |
| **total** | **≈ 22.3 GB = 0.0203 TiB** |

**List-price equivalent ≈ $0.13.** Processing consumption at the published on-demand rate, **not**
money known to have been charged. **Actual monetary cost remains UNKNOWN without billing
evidence.**

Notes worth carrying forward:

- **The dry run was cheap insurance and is now part of the publisher.** 6.9 GB estimated against a
  38.2M-row model before any table was written; the estimate is recorded in
  `docs/phase0/payout_publication.json` next to what was actually billed.
- **Publishing three times cost about as much as publishing once.** The idempotency proof re-runs
  the fact model, and the guard makes it a scan-and-insert-nothing — the expensive part is the
  disposition table, which the re-run does not rebuild.
- **The 972 MB content digest is the price of provable immutability.** Each digest hashes all
  5.9M published rows, and it ran five times (before, during and after the pointer move, plus the
  idempotency comparison). That is roughly 4.8 GB of the phase total, spent entirely on evidence
  rather than on output.
- **The aborted first attempt cost ~3 GB.** The fact table was built once from a disposition model
  that leaked a rate onto held listens, caught by a dbt test, then dropped and rebuilt.

Cumulative project consumption is now roughly **0.98 TiB** across all phases against a 1 TiB
monthly on-demand allowance — so the free tier has probably absorbed all of it, **probably, on
arithmetic, not on billing evidence**.
