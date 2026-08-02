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
TF_ROOTS := bootstrap-state project-foundation raw-storage governance bigquery

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

.PHONY: test test-integration

# Unit tests only: no cloud, no credentials, no cost.
test:
	uv run pytest tests/unit -q

# Integration tests: require GCP + ADC + permission to impersonate the matcher SA.
# These read real tables and bill real bytes.
test-integration:
	uv run pytest tests/integration -v -m integration
