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
