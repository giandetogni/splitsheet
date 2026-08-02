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

Each root has its own state prefix, so no module can read or overwrite another's state,
and a mistake in one cannot plan a change against another's resources.

### What "reproducible from a clean project" now means

Given steps 1–3 above, a fresh clone reaches the current infrastructure with:

```
terraform -chdir=terraform/bootstrap-state    init && terraform -chdir=terraform/bootstrap-state    apply
terraform -chdir=terraform/project-foundation init && terraform -chdir=terraform/project-foundation apply
terraform -chdir=terraform/raw-storage        init && terraform -chdir=terraform/raw-storage        apply
terraform -chdir=terraform/governance         init && terraform -chdir=terraform/governance         apply
```

`project-foundation` must run before `raw-storage` and `governance`, because those two
need `storage.googleapis.com` and `billingbudgets.googleapis.com` respectively.

Bucket names are globally unique, so a different deployment must supply its own via
`terraform.tfvars` (see each root's `terraform.tfvars.example`). `terraform.tfvars` is
git-ignored.

### Why five APIs and not more

`project-foundation` declares only what the project uses **today**:

| API | Used by |
|---|---|
| `serviceusage.googleapis.com` | managing every other enablement |
| `cloudresourcemanager.googleapis.com` | `data.google_project` in `governance` |
| `storage.googleapis.com` | raw bucket, state bucket |
| `cloudbilling.googleapis.com` | billing account reads |
| `billingbudgets.googleapis.com` | the budget in `governance` |

BigQuery, Dataproc and Composer APIs are deliberately absent. Adding them now would turn
this list from a description of the project into a wish, and would enable services that
nothing uses. They are added by the phase that first needs them.

All five carry `disable_on_destroy = false` and `disable_dependent_services = false`:
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
