# Runbook

## The bootstrap boundary

Four things must exist before Terraform can do anything. They are performed once, by a
human, and are **not** managed by this repository. Everything else is.

| # | Manual step | Why it cannot be Terraform |
|---|---|---|
| 1 | **Create the GCP project** (`ss-de-944054e7`) | Project creation needs organisation- or billing-level rights that a project-scoped configuration does not have, and it is the container Terraform authenticates *into*. |
| 2 | **Link the project to a billing account** (`01D939-A7BDBF-1BD0A5`, currency BRL) | Billing linkage is an account-level act requiring Billing Administrator. Without it, service enablement and bucket creation fail. |
| 3 | **Authenticate ADC** — `gcloud auth login` then `gcloud auth application-default login` | Credentials are the input to Terraform, never its output. No key file is used or committed; all roots run on Application Default Credentials. |
| 4 | **Create the state bucket** via `terraform/bootstrap-state` | This root is Terraform, but its own state is local by necessity: it creates the bucket that every other root uses as a backend, and cannot store state in a bucket that does not yet exist. |

Everything past this line is Terraform-managed and reproducible:

| Root module | State | Owns |
|---|---|---|
| `terraform/bootstrap-state` | local (see above) | the Terraform state bucket |
| `terraform/project-foundation` | `gs://…/project-foundation/default.tfstate` | the project's enabled APIs |
| `terraform/raw-storage` | `gs://…/raw-storage/default.tfstate` | the raw preservation bucket |
| `terraform/governance` | `gs://…/governance/default.tfstate` | the monthly budget alert |
| `terraform/bigquery` | `gs://…/bigquery/default.tfstate` | datasets, bronze/eval tables, matcher view and SA |
| `terraform/ci-identity` | `gs://…/ci-identity/default.tfstate` | Workload Identity pool, provider and CI service account |

Each root has its own state prefix, so no module can read or overwrite another's state,
and a mistake in one cannot plan a change against another's resources.

### What "reproducible from a clean project" now means

Given steps 1–3 above, a fresh clone reaches the current infrastructure with:

```
terraform -chdir=terraform/bootstrap-state    init && terraform -chdir=terraform/bootstrap-state    apply
terraform -chdir=terraform/project-foundation init && terraform -chdir=terraform/project-foundation apply
terraform -chdir=terraform/raw-storage        init && terraform -chdir=terraform/raw-storage        apply
terraform -chdir=terraform/governance         init && terraform -chdir=terraform/governance         apply
terraform -chdir=terraform/bigquery           init && terraform -chdir=terraform/bigquery           apply
terraform -chdir=terraform/ci-identity        init && terraform -chdir=terraform/ci-identity        apply
```

`project-foundation` must run before `raw-storage` and `governance`, because those two
need `storage.googleapis.com` and `billingbudgets.googleapis.com` respectively.

Bucket names are globally unique, so a different deployment must supply its own via
`terraform.tfvars` (see each root's `terraform.tfvars.example`). `terraform.tfvars` is
git-ignored.

### Why these APIs and not more

`project-foundation` declares only what the project uses **today**:

| API | Used by |
|---|---|
| `serviceusage.googleapis.com` | managing every other enablement |
| `cloudresourcemanager.googleapis.com` | `data.google_project` in `governance` |
| `storage.googleapis.com` | raw bucket, state bucket |
| `cloudbilling.googleapis.com` | billing account reads |
| `billingbudgets.googleapis.com` | the budget in `governance` |
| `bigquery.googleapis.com` | datasets and tables in `bigquery` |
| `iam.googleapis.com` | the matcher and CI service accounts |
| `iamcredentials.googleapis.com` | impersonation, so no key is ever needed |
| `sts.googleapis.com` | OIDC token exchange for GitHub Actions |

Dataproc and Composer APIs are deliberately absent. Adding them now would turn
this list from a description of the project into a wish, and would enable services that
nothing uses. They are added by the phase that first needs them.

All of them carry `disable_on_destroy = false` and `disable_dependent_services = false`:
destroying one root module must never disable a project-wide service that the other roots
depend on. For `serviceusage` in particular, disabling it would remove the ability to
re-enable anything.

### Adoption, not recreation

These five APIs were originally enabled by hand while earlier phases were being built.
They were brought under management with `terraform import`, so they were never disabled
and re-enabled. `terraform plan` reported **No changes** immediately after import, which
is the evidence that state matches reality and nothing was toggled.

## Data locations

| What | Where | In Git? |
|---|---|---|
| Raw 2026-06 slice, local copy | `~/splitsheet-data/raw/fullexport-2593-2026-06` (mode `0444`) | no |
| Raw 2026-06 slice, cloud copy | `gs://splitsheet-raw-944054e7/raw/listenbrainz/fullexport-2593/period=2026-06/` | no |
| Terraform state backups | `~/splitsheet-data/tfstate-backups/` (mode `0444`, with `.sha256`) | no |
| Slice manifest | `docs/phase0/period_2026_06_manifest.json` | **yes** |
| Recon measurements | `docs/phase0/*.json` | **yes** |

Raw objects contain `user_id`. The buckets are private with public access prevention
enforced, and `user_id` must not appear in any published derivative.

## CI

Two workflows, split by whether they need credentials.

### `ci.yml` — public, credential-free

Triggers on every push and pull request. `permissions: contents: read`, no `id-token`, no
secrets, no path to GCP, so it runs safely on a fork. Steps: `uv lock --check` (explicit,
so an inconsistent lockfile fails unambiguously), `uv sync --frozen --all-groups`, `ruff`,
`make test` (unit only), `make tf-fmt`, `make tf-validate`.

`make tf-validate` runs `terraform init -backend=false` per root, which is what lets it
validate all six roots without touching the remote state bucket or holding any credential.

No dependency cache: install measured under ~10 s, so caching would add moving parts for
no measurable gain.

### `integration.yml` — manual only, OIDC

`workflow_dispatch` only. Authenticates with `google-github-actions/auth` via Workload
Identity Federation. **No service-account key exists anywhere** — verified: 0 user-managed
keys on both service accounts, and no GitHub secrets configured at all.

Federation is pinned three ways, all by immutable numeric ID:

```
assertion.repository_owner_id == "122053316"
assertion.repository_id       == "1320369792"
assertion.ref                 == "refs/heads/main"
```

Names are deliberately not used: a repository name can be re-created by someone else and
an account can be renamed, but these IDs cannot be reassigned. The `principalSet` is
additionally scoped to `attribute.repository_id`, not to the whole pool, so adding another
provider or repository to the pool later grants nothing.

### Blast-radius split

| marker | runs in CI | why |
|---|---|---|
| `integration_readonly` | **yes** | reads only |
| `requires_gcs` | no | needs raw object read; excluded to keep CI's grant narrow |
| `integration_destructive` | **never** | invokes the loader, creates staging, can republish |

CI identity permissions: `bigquery.jobUser` on the project, `metadataViewer` on the two
datasets, `dataViewer` on exactly four tables, `serviceAccountTokenCreator` on the matcher
SA (only so the firewall assertions can run), and `storage.objectViewer` on the raw bucket
(necessity demonstrated — see `terraform/ci-identity/main.tf`). **No write or admin role
anywhere**, and the workflow proves it by attempting a `CREATE TABLE` that must fail.

## Gate patch — CLOSED, status `BLOCKED_BY_GITHUB_PLAN`

Attempted once, before Phase 3. **Not to be retried.** `main` is unprotected and cannot be
protected in this context.

### Evidence

| check | result |
|---|---|
| Classic branch protection | **refused by the API, HTTP 403** |
| Repository rulesets | **refused by the API, HTTP 403** |
| Repository visibility | **private** |
| Plan capability | **does not support enforcement in this context** |

```
GET /repos/giandetogni/splitsheet/branches/main/protection   -> 403
GET /repos/giandetogni/splitsheet/rulesets                   -> 403
"Upgrade to GitHub Pro or make this repository public to enable this feature."
```

Verified by probing the API, not by reading documentation. The message names the gate
itself.

### Decisions taken

- **Repository was not made public.** Publication is gated on criteria this project has not
  met, and visibility is not a lever to be pulled for a CI convenience.
- **No paid upgrade.** No purchase of any kind.
- **No local workaround.** A `pre-push` hook was considered and rejected: it sits outside
  version control's enforcement, is bypassed by `--no-verify`, and is absent from a fresh
  clone. Presenting it as a remote control would be a false claim about the repository's
  guarantees.

### Declared state after the patch

- The **`ci` workflow continues to run on `push` and `pull_request`**, credential-free.
- **Failures are visible but do not block a direct push to `main`.** A push succeeds with
  the `checks` job red. Force-push to `main` and deletion of `main` also remain possible.
  Nothing requires a pull request, and nothing requires a green check before merge.
- The **integration workflow remains `workflow_dispatch` only, read-only, and restricted to
  `main`.** It was not extended to `pull_request`, and no `base_ref` or `head_ref`
  authorisation was added.
- **WIF was not widened.** The provider condition is unchanged:
  `assertion.repository_owner_id == "122053316" && assertion.repository_id == "1320369792"
  && assertion.ref == "refs/heads/main"`. The public workflow grants only `contents: read`
  and no `id-token`.
- **No GCP infrastructure or data was changed.** `terraform plan` reports **No changes**
  across all six roots; no IAM binding, dataset, table, bucket or published row was touched.

Protection therefore rests on operator discipline. CI shortens the time to *notice* a
regression; it does not prevent one. That is the accurate description and should not be
restated as a gate anywhere in this repository.

### If the constraint ever lifts

Should the repository become public or the account move to a paid plan, the intended
configuration is: require pull requests into `main`; require the status check named
**`checks`** (the job id in `ci.yml` — `integration.yml` uses `readonly`, so the two names
never collide); include administrators with no bypass; block force pushes; block branch
deletion; require **no** second reviewer, since the repository has a single author. The GCP
integration workflow must **never** be a required pull-request check, because that would
put `id-token` in reach of unreviewed code. This configuration has never been applied and
is therefore unverified.

## Phase 4B — scored matching

Order is not optional. Canonical texts feed the feature table, the feature table feeds
calibration, and `config/scoring_rules.yml` must be frozen before validation is opened.

```
make phase4b-baseline                       # reclassified cardinality baseline (not the result)
make phase4b-texts GCS_BUCKET=splitsheet-raw-944054e7
make phase4b-features                       # matcher identity, label-blind
make phase4b-analyse                        # evaluation identity, calibration ONLY
make phase4b-calibrate                      # chooses weights/thresholds, calibration ONLY
#   -> update config/scoring_rules.yml, recompute rules_sha256, bump EXPECTED_SCORING_VERSION
make verify                                 # ruff + unit + terraform, exit codes printed
make phase4b-match                          # publishes silver_listen_matches
make phase4b-validate                       # ONE RUN PER scoring_version
make phase4b-unmatched
```

### The validation partition is consumed

`make phase4b-validate` has been run once, under `scoring_version 1.0.0+cb21f9704ff0`. Running
it again does not make the partition blind. If a rule changes:

1. bump `scoring_version` and recompute `rules_sha256` (the loader refuses a stale digest);
2. update `EXPECTED_SCORING_VERSION` in `tests/unit/test_scoring.py` in the same commit;
3. do **not** reuse the existing validation number — establish a new validation strategy or
   wait for new data.

`validate_scoring.py` enforces part of this itself: it compares the `scoring_version` stored on
the published rows with the config on disk and refuses to report if they differ.

### Replacing a published table's schema

`silver_listen_matches` had to lose `tier_confidence` and gain six scoring columns, which
BigQuery cannot do in place. `deletion_protection = true` blocks the destroy, and flipping the
flag in the same apply does not help — the provider evaluates it before the replacement. The
sequence that works, and the order matters:

```
terraform state rm google_bigquery_table.silver_listen_matches
# DROP TABLE via SQL (the content was reproducible from the candidate run)
terraform apply            # recreates with the new schema, deletion_protection = false
# set deletion_protection back to true
terraform apply
terraform plan             # must report No changes
```

Only do this when the table's content is reproducible from a recorded run id. Here the previous
content was the cardinality baseline, which `make phase4b-baseline` republishes into
`baseline_cardinality_matches` from the same `candidate_run_id`.

### Cost note

The evaluation stages cost about 4× the pipeline they measure (275 GB of 389 GB), because each
metric query re-joins the label table to the candidate table. If evaluation is ever run
repeatedly rather than once, materialise the joined evaluation set first.

## Phase 5A — modeled rights and the dbt foundation

```
make phase5a-size                                    # read-only; estimate BEFORE generating
make phase5a-generate GCS_BUCKET=splitsheet-raw-944054e7
make phase5a-snapshot                                # SCD2 baseline -- must precede any revision
make phase5a-build                                   # dbt build: models, tests, quality report
make phase5a-revise  GCS_BUCKET=splitsheet-raw-944054e7   # controlled change + second snapshot
make phase5a-build                                   # refresh dimension and quality report
make phase5a-verify                                  # reproducibility, reconciliation, cost
uv run pytest tests/integration/test_rights_contract.py -m integration_readonly
```

### Order that matters

- **`phase5a-snapshot` before `phase5a-revise`.** The snapshot needs an earlier state to compare
  against; running the revision first means there is no history and SCD2 is unproven.
- **`phase5a-size` before `phase5a-generate`.** Generating millions of rows should be a costed
  decision. The sizing step is read-only and its output is committed.
- The generator refuses to run if the recording universe no longer matches
  `universe.match_run_id` in `config/rights_model.yml`: rights generated against a different
  catalogue would be a different dataset wearing the same version.

### The deliberate defects must survive

`ownership_splits` contains intentional share-sum, overlap, gap, invalid-interval, orphan-recording
and missing-holder defects, and 1,000 holders with no ownership. **Never fix them.** If a staging
model starts filtering or repairing them, the quality layer will report a clean pipeline over
silently corrected data, and `assert_deliberate_defects_survive_into_the_source` will fail — which
is the intended alarm.

### dbt invocation note

Use `../.venv/bin/dbt` rather than `uv run dbt` for long dbt commands on a loaded machine. Two
`dbt snapshot` runs stalled in the parse phase for 10 minutes and 2.5 minutes with no BigQuery job
issued and no log progress past "Partial parsing not enabled"; `vm_stat` showed ~235 MB free. The
same command through the venv binary completed in 15 seconds. It is a local memory-pressure
symptom, not a dbt or profile problem, but it looks exactly like a hung warehouse call, so: check
`ps` and the dbt log before assuming the warehouse is at fault, and never read a timeout as a pass.

### dbt profiles

`dbt/profiles.yml` is committed and contains **no secrets**: `method: oauth` uses the same ADC
identity as every other operational script, and `maximum_bytes_billed` is a hard cap so a runaway
model fails instead of billing. Run with `DBT_PROFILES_DIR` pointing at `dbt/`.

### What Phase 5A deliberately does not do

No payout amount is computed. No gold layer. Streams, share and rate are joined and classified but
never multiplied — that waits until ownership validity is fully proven, which is what this phase
measures.

## Phase 5B — payout policy and immutable financial publication

```
make phase5b-publish     # dry run, publish, prove idempotency, prove pointer moves are safe
make phase5b-rehearse    # a second labelled publication so immutability has two real versions
make phase5b-waterfall   # the reconciliation, all 38,199,641 listens
uv run pytest tests/integration/test_payout_contract.py -m integration_readonly
```

### The publisher refuses more than it does

`src/payout/publish.py` will not run if the warehouse no longer holds the exact inputs named in
`config/payout_policy.yml` — matcher run, scoring version, rights version, rights generation run,
rate rule version. Pricing a different universe under the same `payout_policy_version` would be a
silent restatement, so it is a hard stop rather than a warning.

### Re-publishing is safe; re-building is not

A re-run under the same inputs inserts **zero** rows: `attribution_run_id` is deterministic and the
fact model's guard makes the select empty. So `make phase5b-publish` is safe to repeat.

**`dbt build --full-refresh` is NOT safe.** It would drop and rebuild `fct_royalty_attribution`,
destroying every prior publication. The immutability of a published statement rests on the
append-only strategy and the run-id guard, not on IAM: nothing in the warehouse prevents a
deliberate full refresh. If a real financial system needed this guarantee, the publication would
belong in a Terraform-managed table with `deletion_protection` and no dbt write path. That gap is
recorded rather than implied away.

### Changing the policy

A payout policy change is a new publication, never an edited one:

1. edit `config/payout_policy.yml`, bump `payout_policy_version`, recompute `rules_sha256`;
2. update `EXPECTED_POLICY_VERSION` in `tests/unit/test_payout_policy.py` and the
   `payout_policy_version` var in `dbt/dbt_project.yml` — a unit test asserts the two agree, so the
   warehouse cannot apply one policy while labelling rows with another;
3. run `make phase5b-publish`, which produces a **new** `attribution_run_id` and leaves the previous
   publication intact;
4. move the pointer only when the new figures are approved. Latest is not the same as current.

### What Phase 5B deliberately does not do

No black-box revenue, no restatement, no transliteration, no threshold retuning. `RATE_CARD_GAP`
streams are **never** priced: not at the previous rate, not at an average, not at zero. 3,164,839
streams are held on that basis and the money is simply not created.

## Phase 6 — restatement

```
make phase6-freeze      # register v1 + digest, and produce the black-box report. Do this FIRST.
make phase6-probe       # read-only; measures both alternatives under the frozen selection rule
#   -> implement the chosen rule under a NEW normalization_version, keeping a frozen v1 copy
make verify             # ruff + unit + terraform, exit codes printed
make phase6-reprocess GCS_BUCKET=splitsheet-raw-944054e7
make phase6-publish     # same frozen gates over restated matches -> pub:v2 + delta mart
make phase6-case        # the Agust D / 해금 case, before and after
make phase6-evaluate    # partition inventory + unsupervised observables
uv run pytest tests/integration/test_restatement_contract.py -m integration_readonly
```

### The order is not negotiable

- **Freeze before anything.** Every later step re-checks v1's digest against the registry and stops
  if it moved.
- **Trigger config before the probe.** `config/restatement_trigger.yml` fixes the alternatives and
  the selection rule; a probe that chose its own criteria afterwards would be an argument, not a
  measurement.
- **Probe before implementing.** The rule that shipped is the one the probe selected under the frozen
  criteria, and the losing alternative is recorded next to it.

### Two normalization versions now exist

| | |
|---|---|
| `config/normalization_rules_v1.0.0.yml` | FROZEN COPY, `1.0.0+0bc0dd643e06`. Never edit it: it is what makes pub:v1 reproducible. |
| `config/normalization_rules.yml` | live, `1.1.0+b3253b155934`, with the transliteration block. |

`tests/unit/test_frozen.py` asserts both — the frozen copy still loads to the v1 version, and the
live config equals the version declared in `config/frozen_versions.yml`. A third, undeclared version
fails the suite.

### What must never happen to a publication

`dbt build --full-refresh` on `fct_royalty_attribution` or `fct_restatements` now **fails at compile
time** with an explanatory error, because a full refresh would drop every published statement
including the frozen baseline. That guard is a Jinja check, not IAM: it stops the accident, not a
determined operator. Re-publishing under the same inputs is already a no-op, so a full refresh is
never the way to republish.

### Restatement run ids

`restatement_run_id` is derived from the normalization version, the scoring version, the v1 match run
and the blocking version. It does **not** include the cohort predicate, so two runs with different
cohort definitions under the same versions would share an id. The cohort definition is frozen in
`config/restatement_trigger.yml` and recorded in the run's own artifact, which is how they are told
apart today; folding it into the hash is a small, worthwhile change for the next restatement.

### dbt invocation

Use `.venv/bin/dbt` and `.venv/bin/python` rather than `uv run` for long commands: several `uv run`
invocations stalled in startup under memory pressure with no BigQuery job issued. A timeout is never
read as success here — check `ps` and the job list before concluding anything.

---

## Phase 7A — orchestration foundation

```bash
make dag-graph      # task ids, stages, retries and edges. No Airflow, no credentials.
make dag-check      # the DAG's shape, asserted by the unit suite
make airflow-up     # local UI at :8080. Pulls ~1 GiB of image; check disk first.
```

The graph lives in `dags/pipeline_spec.py`, which imports neither Airflow nor any Google
library. `dags/splitsheet_monthly_pipeline.py` is a thin adapter that turns each declared
task into a `BashOperator`. That split is what lets `make dag-check` assert the task ids,
the edges, the retry policy and the rendered commands on a machine with no Airflow
installed — the DagBag import test is present but skips when Airflow is absent, and is
never reported as a pass it did not earn.

Every task invokes a CLI that already exists and is already documented above. The DAG
owns ordering, parameters, retries and run identity, and nothing else. A test asserts
that each `src/**.py` a task names is a real file, so a decorative wrapper fails the suite.

One deliberate deviation from the suggested task list in `PROJECT_SPEC.md`: its item 13,
`generate_quality_report`, is here `verify_rights_layer`. The CLI it maps to,
`src/rights/verify_rights.py`, verifies the Phase 5A rights layer — reproducibility,
reconciliation, temporal proof, cost — and produces no quality report. The quality report
is `dbt/models/quality/quality_report.sql`, already materialised by `run_dbt_build`
upstream. The task is named for what it does; writing a second generator to justify the
original name would have been a wrapper, which is the thing this phase is not allowed.

### What the DAG may not do

- **Publication is off by default.** `publish_period_results` begins with a shell guard
  that exits 1 unless `allow_publication` is set to `True` for that run. It fails loudly
  rather than skipping: a run that silently declined to publish would be indistinguishable
  from one that published. `retries=0`, so no scheduler can repeat it.
- **`validate_scoring.py` is deliberately not in the graph.** It may run once per
  `scoring_version` — re-running it does not make the validation partition blind again,
  so it is not a thing a retrying scheduler may hold. `top_unmatched.py`, which is
  read-only, covers `validate_match_completeness` instead.
- **No download tasks.** The 2026-06 slice is preserved and immutable; re-fetching it
  monthly would re-derive an artifact whose whole point is that it does not change.
  `verify_source_slice` and `verify_gcs_slice` cover source availability.
- **Writes are *directed* to `artifacts/airflow/<ts_nodash>/`, not confined there.**
  Every task's `--out` renders under that directory and `evidence_dir` is only ever read,
  as `--manifest`; the render tests assert both. That is a property of the declared
  commands, not a boundary: the repo is bind-mounted read-write, so a task that named
  `docs/phase0` in its `--out` would succeed, and the current test would not catch it.
  What protects committed evidence today is that no task names it, plus `artifacts/` in
  `.gitignore` keeping run output out of the tree. An enforced boundary is 7B's.

Known gap for 7B: the tasks call `uv run python`, while the dbt note above records that
`uv run` stalled under host memory pressure. Inside the container that pressure does not
apply, but the first real container run is what settles it.

Two things remain **unproven for want of a dependency and of disk**, and are not claimed
either way: `dags/splitsheet_monthly_pipeline.py` has never been imported under Airflow
(the DagBag test skips, and `apache-airflow` is deliberately not installed here), and the
container has never been started (`make airflow-up` wants ~1 GiB of image against 1.9 GiB
free). Both close in 7B, on a host with room. Until then the honest claim is a tested task
graph, not a running DAG.
