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
