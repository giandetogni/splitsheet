DATA_DIR ?= data/raw
OUT_DIR  ?= docs/phase0
LISTENS  := $(DATA_DIR)/listenbrainz-listens-dump-2611-20260730-000002-incremental.tar.zst
CANON    := $(DATA_DIR)/musicbrainz-canonical-dump-20260717-080003.tar.zst
LMEMBER  := listenbrainz-listens-dump-2611-20260730-000002-incremental/listens/2026/7.listens
CMEMBER  := musicbrainz-canonical-dump-20260717-080003/canonical/canonical_musicbrainz_data.csv

KEYS     ?= $(DATA_DIR)/keys_202607.json
PERIOD   ?= 2026-07
PERIOD_TARGET ?= 2026-06

# Raw slice lives outside the repository on purpose: it is private, immutable and
# git-ignored. Only the manifest is committable.
RAW_DIR  ?= $(HOME)/splitsheet-data/raw/fullexport-2593-2026-06
MANIFEST ?= $(OUT_DIR)/period_2026_06_manifest.json

.PHONY: phase0-fetch phase0-profile phase0-candidates phase0b-index phase0b-period \
        phase0b-extract phase0b-verify phase0

phase0-fetch:
	DATA_DIR=$(DATA_DIR) bash src/recon/fetch_phase0_samples.sh

# Streams each artifact through the profiler: the decompressed listens file is 2.2 GB
# and the canonical CSV is 7.5 GB, neither of which is written to disk.
phase0-profile:
	mkdir -p $(OUT_DIR)
	zstd -dc $(LISTENS) | tar -xO -f - $(LMEMBER) \
	  | python3 src/recon/profile_listens.py --out $(OUT_DIR)/listens_2611_full.json
	zstd -dc $(LISTENS) | tar -xO -f - $(LMEMBER) \
	  | python3 src/recon/profile_listens.py --only-month 2026-07 \
	      --out $(OUT_DIR)/listens_2611_202607.json
	zstd -dc $(CANON) | tar -xO -f - $(CMEMBER) \
	  | python3 src/recon/profile_canonical.py --out $(OUT_DIR)/canonical_profile.json

# Sizes the listen-to-canonical candidate space. Two stages because the two sides
# are separate streams; the intermediate keys file is scratch, not an artifact.
phase0-candidates:
	mkdir -p $(OUT_DIR)
	zstd -dc $(LISTENS) | tar -xO -f - $(LMEMBER) \
	  | python3 src/recon/probe_candidate_space.py keys \
	      --only-month $(PERIOD) --out $(KEYS)
	zstd -dc $(CANON) | tar -xO -f - $(CMEMBER) \
	  | python3 src/recon/probe_candidate_space.py join \
	      --keys $(KEYS) --out $(OUT_DIR)/candidate_space.json

# Phase 0B: index the 191 GiB full export and locate a period over HTTP Range.
# Downloads nothing; total transfer is well under 1% of the archive.
phase0b-index:
	mkdir -p $(OUT_DIR)
	uv run python src/recon/index_fullexport.py probe --members 6
	uv run python src/recon/index_fullexport.py walk --out $(OUT_DIR)/fullexport_index.json

phase0b-period:
	uv run python src/recon/locate_period.py --index $(OUT_DIR)/fullexport_index.json \
	  --period $(PERIOD_TARGET) --out $(OUT_DIR)/period_$(subst -,_,$(PERIOD_TARGET)).json

# Preserve the raw slice. Idempotent: members already present with the manifest's
# size and SHA-256 are not re-downloaded.
phase0b-extract:
	mkdir -p $(RAW_DIR)
	uv run python src/recon/extract_period.py \
	  --index $(OUT_DIR)/fullexport_index.json \
	  --period-report $(OUT_DIR)/period_2026_06.json \
	  --raw-dir $(RAW_DIR) --manifest $(MANIFEST)

phase0b-verify:
	uv run python src/recon/verify_slice.py --manifest $(MANIFEST) \
	  --raw-dir $(RAW_DIR) --out $(OUT_DIR)/period_2026_06_verification.json

phase0: phase0-fetch phase0-profile phase0-candidates

# ---------------------------------------------------------------------------
# Phase 1A: second, verified copy of the preserved slice in GCS.
# Requires ADC (gcloud auth application-default login) and GCS_BUCKET.
# ---------------------------------------------------------------------------
TF_RAW   := terraform/raw-storage
GCS_BUCKET ?=

.PHONY: phase1a-check phase1a-fmt phase1a-validate phase1a-plan phase1a-apply \
        phase1a-upload phase1a-verify

# Pre-apply gate: fails loudly rather than letting apply discover the problem.
phase1a-check:
	@gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . \
	  || (echo "no active gcloud account: run 'gcloud auth login'" && exit 1)
	@test -f "$$HOME/.config/gcloud/application_default_credentials.json" \
	  || (echo "no ADC: run 'gcloud auth application-default login'" && exit 1)
	@gcloud config get-value project 2>/dev/null | grep -q . \
	  || (echo "no project set: run 'gcloud config set project <id>'" && exit 1)
	@echo "account : $$(gcloud auth list --filter=status:ACTIVE --format='value(account)')"
	@echo "project : $$(gcloud config get-value project 2>/dev/null)"
	@gcloud billing projects describe "$$(gcloud config get-value project 2>/dev/null)" \
	  --format='value(billingEnabled)' | grep -q True \
	  || (echo "billing NOT enabled on project" && exit 1)
	@echo "billing : enabled"

phase1a-fmt:
	terraform -chdir=$(TF_RAW) fmt -check -recursive

phase1a-validate:
	terraform -chdir=$(TF_RAW) init -backend=false -input=false >/dev/null
	terraform -chdir=$(TF_RAW) validate

phase1a-plan:
	terraform -chdir=$(TF_RAW) init -input=false >/dev/null
	terraform -chdir=$(TF_RAW) plan -input=false

phase1a-apply:
	terraform -chdir=$(TF_RAW) apply -input=false

phase1a-upload:
	@test -n "$(GCS_BUCKET)" || (echo "set GCS_BUCKET=<bucket-name>" && exit 1)
	uv run python src/preservation/upload_slice.py --manifest $(MANIFEST) \
	  --raw-dir $(RAW_DIR) --bucket $(GCS_BUCKET) \
	  --out $(OUT_DIR)/gcs_upload_report.json

phase1a-verify:
	@test -n "$(GCS_BUCKET)" || (echo "set GCS_BUCKET=<bucket-name>" && exit 1)
	uv run python src/preservation/verify_gcs_slice.py --manifest $(MANIFEST) \
	  --raw-dir $(RAW_DIR) --bucket $(GCS_BUCKET) \
	  --out $(OUT_DIR)/gcs_verification.json

# ---------------------------------------------------------------------------
# All Terraform roots. Ordered: project-foundation enables the APIs the other
# roots need, so it must be applied before raw-storage and governance.
# ---------------------------------------------------------------------------
TF_ROOTS := bootstrap-state project-foundation raw-storage governance bigquery ci-identity

.PHONY: tf-fmt tf-validate tf-plan

tf-fmt:
	@for d in $(TF_ROOTS); do \
	  printf '%-22s ' "$$d"; \
	  terraform -chdir=terraform/$$d fmt -check -recursive >/dev/null 2>&1 \
	    && echo PASS || { echo FAIL; exit 1; }; \
	done

# -backend=false so this runs with no GCP credentials at all, which is what lets the
# public CI job validate every root without touching the remote state bucket.
tf-validate:
	@for d in $(TF_ROOTS); do \
	  printf '%-22s ' "$$d"; \
	  terraform -chdir=terraform/$$d init -backend=false -input=false >/dev/null 2>&1 || \
	    { echo "FAIL (init)"; exit 1; }; \
	  terraform -chdir=terraform/$$d validate 2>&1 | grep -q "configuration is valid" \
	    && echo PASS || { echo FAIL; exit 1; }; \
	done

tf-plan:
	@for d in $(TF_ROOTS); do \
	  printf '%-22s ' "$$d"; \
	  terraform -chdir=terraform/$$d plan -input=false 2>&1 | grep -qE "No changes" \
	    && echo "No changes" || echo "CHANGES"; \
	done

.PHONY: test test-integration test-integration-readonly

# Unit tests only: no cloud, no credentials, no cost.
test:
	uv run pytest tests/unit -q

# Integration tests: require GCP + ADC + permission to impersonate the matcher SA.
# These read real tables and bill real bytes.
test-integration:
	uv run pytest tests/integration -v -m integration

# The subset CI is allowed to run: reads only, no GCS object access.
test-integration-readonly:
	uv run pytest tests/integration -v -m "integration_readonly and not requires_gcs"

# ---------------------------------------------------------------------------
# One command, run before every commit. Fails immediately, suppresses nothing,
# and prints each exit code so a killed process (137), a timeout or missing
# output can never be mistaken for success.
# ---------------------------------------------------------------------------
.PHONY: verify

verify:
	@set -e; \
	fail=0; \
	printf '\n== ruff ==\n';            uv run ruff check src tests; rc=$$?; echo "exit=$$rc"; [ $$rc -eq 0 ] || fail=1; \
	printf '\n== unit tests ==\n';      uv run pytest tests/unit -q;  rc=$$?; echo "exit=$$rc"; [ $$rc -eq 0 ] || fail=1; \
	printf '\n== terraform fmt ==\n';   $(MAKE) --no-print-directory tf-fmt;      rc=$$?; echo "exit=$$rc"; [ $$rc -eq 0 ] || fail=1; \
	printf '\n== terraform validate ==\n'; $(MAKE) --no-print-directory tf-validate; rc=$$?; echo "exit=$$rc"; [ $$rc -eq 0 ] || fail=1; \
	printf '\n'; \
	if [ $$fail -ne 0 ]; then echo "VERIFY FAILED"; exit 1; fi; \
	echo "VERIFY OK (all four steps exited 0)"

# ---------------------------------------------------------------------------
# Phase 4B: scored matching. Order matters -- canonical texts feed the feature
# table, the feature table feeds calibration, and the config must be frozen
# before validation is opened. `phase4b-validate` consumes the validation
# partition and may only be run once per scoring_version.
# ---------------------------------------------------------------------------
NORM_VERSION      ?= 1.0.0+0bc0dd643e06
BLOCKING_VERSION  ?= staged-1.0.0
CANDIDATE_RUN_ID  ?= blk:c005e9a56b1ec542
DERIVED_DIR       ?= $(HOME)/splitsheet-data/derived

.PHONY: phase4b-baseline phase4b-texts phase4b-features phase4b-analyse \
        phase4b-calibrate phase4b-match phase4b-validate phase4b-unmatched

# The Phase 4A artifact, reclassified: cardinality only, no score, no confidence.
phase4b-baseline:
	uv run python src/matching/build_baseline_cardinality.py --project ss-de-944054e7 \
	  --norm-version "$(NORM_VERSION)" --blocking-version "$(BLOCKING_VERSION)" \
	  --candidate-run-id "$(CANDIDATE_RUN_ID)" \
	  --out $(OUT_DIR)/baseline_cardinality_run.json

phase4b-texts:
	uv run python src/normalization/build_canonical_texts.py \
	  --candidate-run-id "$(CANDIDATE_RUN_ID)" --bucket $(GCS_BUCKET) \
	  --work-dir $(DERIVED_DIR) --out $(OUT_DIR)/canonical_texts_run.json

phase4b-features:
	uv run python src/matching/build_candidate_features.py \
	  --norm-version "$(NORM_VERSION)" --candidate-run-id "$(CANDIDATE_RUN_ID)" \
	  --out $(OUT_DIR)/candidate_features_run.json

# Evaluation identity, calibration partition only.
phase4b-analyse:
	uv run python src/evaluation/analyze_features.py \
	  --out $(OUT_DIR)/feature_analysis_calibration.json

phase4b-calibrate:
	uv run python src/evaluation/calibrate.py \
	  --out $(OUT_DIR)/calibration_tradeoff.json

phase4b-match:
	uv run python src/matching/build_match_results.py \
	  --norm-version "$(NORM_VERSION)" --blocking-version "$(BLOCKING_VERSION)" \
	  --candidate-run-id "$(CANDIDATE_RUN_ID)" \
	  --out $(OUT_DIR)/match_results_scored_run.json

# ONE RUN PER scoring_version. Re-running it does not make the partition blind again.
phase4b-validate:
	uv run python src/evaluation/validate_scoring.py \
	  --candidate-run-id "$(CANDIDATE_RUN_ID)" \
	  --out $(OUT_DIR)/validation_run_once.json

phase4b-unmatched:
	uv run python src/evaluation/top_unmatched.py --out $(OUT_DIR)/top_unmatched.json

# ---------------------------------------------------------------------------
# Phase 5A: MODELED rights, temporal validity and the dbt foundation.
#
# Rights holders, ownership splits and rate cards are MODELED, generated from a
# versioned seed. ListenBrainz listens and MusicBrainz recordings are REAL. Any
# royalty amount derived from this data is an illustrative modeled amount.
#
# Order: size -> generate -> snapshot -> build. The first snapshot must run
# BEFORE the revision, or there is no earlier state for SCD2 to detect.
# ---------------------------------------------------------------------------
DBT_DIR ?= dbt
DBT     := DBT_PROFILES_DIR=$(abspath $(DBT_DIR)) uv run dbt

.PHONY: phase5a-size phase5a-generate phase5a-snapshot phase5a-revise phase5a-build \
        phase5a-verify dbt-build dbt-test dbt-parse

# Read-only. Measures the payout universe and estimates the rights volume BEFORE generating.
phase5a-size:
	uv run python src/rights/size_rights.py --out $(OUT_DIR)/rights_sizing.json

phase5a-generate:
	uv run python src/rights/build_rights.py --bucket $(GCS_BUCKET) \
	  --work-dir $(DERIVED_DIR)/rights --out $(OUT_DIR)/rights_generation.json

# Baseline state for SCD Type 2. Run this before any revision.
phase5a-snapshot:
	cd $(DBT_DIR) && $(DBT) snapshot

# Controlled change for the SCD2 proof: re-lands ONLY rights_holders with N payee_status
# values flipped. Run phase5a-snapshot again afterwards to capture the second version.
phase5a-revise:
	uv run python src/rights/build_rights.py --bucket $(GCS_BUCKET) \
	  --work-dir $(DERIVED_DIR)/rights --revise-payee-status 250 \
	  --out $(OUT_DIR)/rights_revision_run.json
	cd $(DBT_DIR) && $(DBT) snapshot

phase5a-build dbt-build:
	cd $(DBT_DIR) && $(DBT) build

dbt-test:
	cd $(DBT_DIR) && $(DBT) test

# Credential-free: parses every ref, source, macro and YAML contract without a warehouse
# connection. This is the dbt check public CI runs.
dbt-parse:
	cd $(DBT_DIR) && $(DBT) parse

# Records what was built, at what cost, and reconciles injected against detected defects.
phase5a-verify:
	uv run python src/rights/verify_rights.py --out $(OUT_DIR)/rights_verification.json

# ---------------------------------------------------------------------------
# Phase 5B: payout policy, royalty attribution, immutable publication.
#
# MATCHED != PAYABLE. Every amount is an ILLUSTRATIVE MODELED AMOUNT: the rate
# card and ownership splits are MODELED. The matcher and the rights data are
# frozen; nothing here may change them.
#
# `phase5b-publish` is the real financial publication. It is idempotent: a
# re-run under the same inputs inserts zero rows, because attribution_run_id is
# deterministic and the fact model refuses a run it already holds.
# ---------------------------------------------------------------------------
.PHONY: phase5b-publish phase5b-rehearse phase5b-waterfall

# Dry-runs the expensive models, publishes, then proves idempotency and that
# moving the current pointer destroys nothing.
phase5b-publish:
	uv run python src/payout/publish.py --out $(OUT_DIR)/payout_publication.json \
	  --prove-idempotency --prove-pointer-move

# A second, clearly labelled publication that coexists with the first. NOT a
# financial statement: it exists so immutability and pointer movement are proven
# with two real publications instead of one.
phase5b-rehearse:
	uv run python src/payout/publish.py --label REHEARSAL --skip-dry-run \
	  --out $(OUT_DIR)/payout_publication_rehearsal.json

phase5b-waterfall:
	@uv run python -c "from google.cloud import bigquery; c=bigquery.Client(project='ss-de-944054e7'); \
	[print(f\"  {r.attribution_status:<22} listens={r.listens:>12,}  {r.pct_of_all_listens:>10}%\") \
	 for r in c.query('SELECT attribution_status, listens, pct_of_all_listens FROM \`ss-de-944054e7.splitsheet_dbt.royalty_reconciliation\` ORDER BY listens DESC').result()]"

# ---------------------------------------------------------------------------
# Phase 6: black-box quantification and restatement.
#
# The v1 financial publication is an IMMUTABLE BASELINE. Every target below
# reads it; none modifies it. `phase6-freeze` records its digest first, and
# every later step re-checks that digest.
#
# Order matters: freeze -> probe -> reprocess -> publish -> case -> evaluate.
# The probe must run BEFORE any rule is implemented, and the trigger config
# must be frozen before the probe.
# ---------------------------------------------------------------------------
# THE LEGACY IDENTIFIER, and it stays. It is what the published pub:v2 rows carry, and
# attribution_run_id is a hash of it -- publishing under the canonical
# `restate:v1:2f08f786d0d3e552` would mint a THIRD publication rather than correct anything.
# The mapping between the two lives in dbt/models/finance/restatement_run_registry.sql, and
# publish_restatement.py refuses any identity that would create a successor publication unless
# --allow-new-publication is passed. Leave this unset to reproduce the published statement.
RESTATEMENT_RUN_ID ?= restate:6b3923771883e860
NEW_NORM_VERSION   ?= 1.1.0+b3253b155934
PRIOR_RUN_ID       ?= attr:83c013596d1d3da3

.PHONY: phase6-freeze phase6-probe phase6-reprocess phase6-publish phase6-case \
        phase6-evaluate phase6-registry

# Freezes v1 with a reproducible digest and produces the black-box decomposition.
phase6-freeze:
	.venv/bin/python src/restatement/freeze_baseline.py \
	  --out $(OUT_DIR)/baseline_v1_freeze.json

# Read-only. Measures both transliteration alternatives and applies the frozen selection rule.
phase6-probe:
	.venv/bin/python src/restatement/probe_transliteration.py \
	  --out $(OUT_DIR)/transliteration_probe.json

# Reprocesses ONLY the affected cohort under the new normalization version and proves that
# everything outside it is byte-identical to v1.
phase6-reprocess:
	.venv/bin/python src/restatement/reprocess_cohort.py --bucket $(GCS_BUCKET) \
	  --work-dir $(DERIVED_DIR)/restatement --out $(OUT_DIR)/restatement_reprocess.json

# Runs the SAME frozen financial gates over the restated matches, publishes v2, builds the delta
# mart, and proves v1 is untouched throughout.
phase6-publish:
	.venv/bin/python src/restatement/publish_restatement.py \
	  --restatement-run-id "$(RESTATEMENT_RUN_ID)" \
	  --new-normalization-version "$(NEW_NORM_VERSION)" \
	  --out $(OUT_DIR)/restatement_publication.json

# Regenerates the restatement run registry model from config/restatement_cohorts.yml and
# src/restatement/identity.py. Pure: no cloud. The unit suite fails if the checked-in model
# is stale, so this is the only way the warehouse's copy is allowed to change.
phase6-registry:
	cd src && ../.venv/bin/python -m restatement.registry --write

# The Agust D / 해금 case, before and after, reported as measured even if it fails.
phase6-case:
	.venv/bin/python src/restatement/concrete_case.py --prior-run-id "$(PRIOR_RUN_ID)" \
	  --new-run-id "$(NEW_NORM_VERSION)" --out $(OUT_DIR)/restatement_concrete_case.json

# Inventories whether an untouched reference partition exists (it does not) and reports
# unsupervised observables plus one clearly-labelled reference comparison.
phase6-evaluate:
	.venv/bin/python src/restatement/evaluate_restatement.py \
	  --out $(OUT_DIR)/restatement_evaluation.json

# ---------------------------------------------------------------------------
# Phase 7A: orchestration foundation. The DAG coordinates the CLIs above; it
# contains no matching, payout or restatement logic.
#
# `dag-graph` and `dag-check` need no Airflow install and no credentials: the
# task graph lives in dags/pipeline_spec.py, which imports neither.
# ---------------------------------------------------------------------------
.PHONY: dag-graph dag-check airflow-up airflow-down

dag-graph:
	@uv run python -c "import sys; sys.path.insert(0,'dags'); import pipeline_spec as s; \
	[print(f'{t.stage:<12} {t.task_id:<28} retries={t.retries}  <- {\", \".join(t.upstream) or \"(entry)\"}') for t in s.TASKS]"

dag-check:
	uv run pytest tests/unit/test_dag_spec.py -q

# Pulls ~1 GiB of image on first run. Check free disk before invoking.
airflow-up:
	docker compose -f docker/airflow-compose.yml up -d

airflow-down:
	docker compose -f docker/airflow-compose.yml down
