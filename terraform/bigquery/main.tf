terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  backend "gcs" {
    bucket = "splitsheet-tfstate-944054e7"
    prefix = "bigquery"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  billing_project       = var.project_id
  user_project_override = true
}

# Bronze: ingested listens, one dataset per layer.
#
# Location must match the GCS bucket holding the slice (US-CENTRAL1), because a load or
# external read cannot cross locations. That is why region is effectively immutable here
# too: moving it means recreating both the bucket and every dataset.
resource "google_bigquery_dataset" "bronze" {
  dataset_id  = "splitsheet_bronze"
  project     = var.project_id
  location    = var.region
  description = "Ingested ListenBrainz listens for the preserved 2026-06 slice. Contains no user_id and no mapper reference label."

  # Deleting a dataset that still has tables must fail rather than take the tables with it.
  delete_contents_on_destroy = false

  labels = {
    project = "splitsheet"
    layer   = "bronze"
    phase   = "2a"
  }
}

# Evaluation: physically separate dataset for the ListenBrainz mapper reference label.
#
# Separation is the enforcement mechanism for the evaluation policy. The label must never
# be reachable from a matcher input, a blocking key, a scoring feature, a filter or a
# tie-breaker. Keeping it in its own dataset means using it is always a deliberate,
# visible cross-dataset join rather than a column that happens to be in scope.
resource "google_bigquery_dataset" "eval" {
  dataset_id  = "splitsheet_eval"
  project     = var.project_id
  location    = var.region
  description = "ListenBrainz mapper reference labels. Evaluation only: never an input to matching. Not ground truth."

  delete_contents_on_destroy = false

  labels = {
    project = "splitsheet"
    layer   = "eval"
    phase   = "2a"
  }
}

# No silver or gold datasets: they belong to the phases that produce them.
# No service accounts: access is the developer's own ADC identity.
# No clustering anywhere: there is no evidence yet that any clustering key helps, and
# clustering chosen without measurement is the ornamentation this project avoids.

# Source contract: the external table is bound to the manifest, not to a glob.
#
# A wildcard would silently absorb any future .parquet dropped under the prefix, which
# means the table's contents could change without a code change. Reading the manifest
# here makes the 23 members an explicit, reviewable list, and Terraform fails outright if
# the manifest and the bucket ever disagree in count.
locals {
  manifest     = jsondecode(file("${path.module}/../../docs/phase0/period_2026_06_manifest.json"))
  slice_prefix = "raw/listenbrainz/fullexport-2593/period=2026-06"
  member_uris = [
    for m in local.manifest.members :
    "gs://${var.raw_bucket_name}/${local.slice_prefix}/${m.member}"
  ]
}

# Fails the plan if the manifest ever stops describing exactly 23 members.
check "manifest_member_count" {
  assert {
    condition     = length(local.member_uris) == 23
    error_message = "Expected exactly 23 members in the slice manifest, found ${length(local.member_uris)}."
  }
}

# External table over the 23 preserved members, enumerated explicitly.
#
# External rather than loaded because the authoritative copy is the GCS slice, already
# checksum-verified. Reading it in place keeps exactly one copy of the raw bytes in the
# cloud and makes _FILE_NAME available, which is the only way to record which source
# member a row came from.
resource "google_bigquery_table" "ext_listens_2026_06" {
  dataset_id          = google_bigquery_dataset.bronze.dataset_id
  table_id            = "ext_listens_2026_06"
  project             = var.project_id
  deletion_protection = false # external table holds no data of its own

  description = "Read-only view of the preserved 2026-06 slice. Bound to the 23 URIs in period_2026_06_manifest.json; no wildcard. 39,200,000 rows, of which 38,199,641 fall inside the period."

  external_data_configuration {
    autodetect    = true
    source_format = "PARQUET"
    source_uris   = local.member_uris
  }
}

resource "google_bigquery_table" "bronze_listens" {
  dataset_id          = google_bigquery_dataset.bronze.dataset_id
  table_id            = "bronze_listens"
  project             = var.project_id
  deletion_protection = true

  description = "One row per listen in [2026-06-01, 2026-07-01). Raw submitted strings only: contains no user_id and no ListenBrainz-derived identifier."

  time_partitioning {
    type  = "DAY"
    field = "listened_at"
  }

  # Top-level form; the time_partitioning-nested field is deprecated.
  require_partition_filter = true

  # No clustering: no measurement yet justifies a clustering key.

  schema = jsonencode([
    { name = "listen_hash", type = "STRING", mode = "REQUIRED", description = "SHA-256 of (user_id, listened_at, recording_msid). Pseudonymous surrogate key, unique across all 38,199,641 rows (measured: zero collisions). Also the join key to splitsheet_eval.mapper_reference_labels." },
    { name = "listened_at", type = "TIMESTAMP", mode = "REQUIRED", description = "Listen time, UTC. Partition column. Source Parquet encodes this as INT96; verified lossless into TIMESTAMP." },
    { name = "submitted_at", type = "TIMESTAMP", mode = "NULLABLE", description = "When ListenBrainz stored the listen (source column 'created'). Enables late-arrival analysis." },
    { name = "recording_msid", type = "STRING", mode = "REQUIRED", description = "ListenBrainz identifier for the (artist string, track string) pair. Not a MusicBrainz ID." },
    { name = "artist_name", type = "STRING", mode = "REQUIRED", description = "Raw submitted artist string. 100% populated." },
    { name = "recording_name", type = "STRING", mode = "REQUIRED", description = "Raw submitted track string. 100% populated." },
    { name = "release_name", type = "STRING", mode = "NULLABLE", description = "Raw submitted release string. 97.154% populated; retained because it is submitted, not derived." },
    { name = "source_file", type = "STRING", mode = "REQUIRED", description = "GCS object the row came from, via _FILE_NAME." },
    { name = "dump_id", type = "STRING", mode = "REQUIRED", description = "Upstream artifact identifier." },
    { name = "ingestion_run_id", type = "STRING", mode = "REQUIRED", description = "Deterministic per slice content, so a re-run produces the same value." },
    { name = "ingested_at", type = "TIMESTAMP", mode = "REQUIRED", description = "Wall-clock time of the materialisation run." },
  ])
}

# The ListenBrainz mapper reference label, physically isolated in its own dataset.
#
# Separation is the enforcement mechanism, not a convention: reaching this table always
# requires an explicit cross-dataset join, so it cannot leak into a matcher input,
# blocking key, scoring feature, filter or tie-breaker by accident. Carries no columns
# beyond the join key, the label and its availability flag -- anything else would risk
# becoming a feature.
resource "google_bigquery_table" "mapper_reference_labels" {
  dataset_id          = google_bigquery_dataset.eval.dataset_id
  table_id            = "mapper_reference_labels"
  project             = var.project_id
  deletion_protection = true

  description = "ListenBrainz mapper reference labels for evaluation only. NOT ground truth: provenance is strong structural evidence, and the mapper's errors correlate with any string matcher's. Never an input to matching."

  schema = jsonencode([
    { name = "listen_hash", type = "STRING", mode = "REQUIRED", description = "Join key to splitsheet_bronze.bronze_listens." },
    { name = "mapper_recording_mbid", type = "STRING", mode = "NULLABLE", description = "ListenBrainz-derived recording MBID. Evaluation only." },
    { name = "label_available", type = "BOOL", mode = "REQUIRED", description = "TRUE when the mapper produced an MBID. Metrics must be reported only over the labelled subset." },
  ])
}

# ---------------------------------------------------------------------------
# Evaluation firewall
# ---------------------------------------------------------------------------

# The matcher's ONLY input contract.
#
# A view rather than direct table access, so the column list is enforced by the object
# itself: the matcher literally cannot select a column that is not projected here. Every
# ListenBrainz-derived identifier is absent by construction.
resource "google_bigquery_table" "v_matcher_input" {
  dataset_id          = google_bigquery_dataset.bronze.dataset_id
  table_id            = "v_matcher_input"
  project             = var.project_id
  deletion_protection = false

  description = "The matcher's only permitted input. Raw submitted strings plus lineage columns. No user_id, no ListenBrainz-derived identifier, no reference label."

  view {
    use_legacy_sql = false
    query          = <<-SQL
      SELECT
        listen_hash,
        listened_at,
        artist_name,
        recording_name,
        release_name,
        recording_msid,
        source_file,
        dump_id,
        ingestion_run_id
      FROM `${var.project_id}.splitsheet_bronze.bronze_listens`
      WHERE listened_at >= TIMESTAMP '2026-06-01 00:00:00+00'
        AND listened_at <  TIMESTAMP '2026-07-01 00:00:00+00'
    SQL
  }
}

# Dedicated identity for the matcher. No JSON key is created here or anywhere: callers
# impersonate it, so there is no long-lived secret to leak or commit.
resource "google_service_account" "matcher" {
  account_id   = "splitsheet-matcher"
  display_name = "SplitSheet matcher"
  description  = "Runs matching against v_matcher_input. Deliberately has no access to splitsheet_eval."
  project      = var.project_id
}

# Enough to run a query job, and nothing more. Not dataViewer at project level, not any
# admin role.
resource "google_project_iam_member" "matcher_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.matcher.email}"
}

# Read access is granted on the VIEW ALONE, not on the bronze dataset. This is the
# difference between "the matcher can read its contract" and "the matcher can read
# everything that happens to live next to its contract".
resource "google_bigquery_table_iam_member" "matcher_reads_view" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.bronze.dataset_id
  table_id   = google_bigquery_table.v_matcher_input.table_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.matcher.email}"
}

# Authorising the view lets it read bronze_listens on the caller's behalf, so the matcher
# never needs access to the underlying table itself.
resource "google_bigquery_dataset_access" "authorize_matcher_view" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.bronze.dataset_id

  view {
    project_id = var.project_id
    dataset_id = google_bigquery_dataset.bronze.dataset_id
    table_id   = google_bigquery_table.v_matcher_input.table_id
  }
}

# Lets the human identity impersonate the matcher, which is how the firewall is tested
# without ever minting a key.
resource "google_service_account_iam_member" "human_can_impersonate" {
  service_account_id = google_service_account.matcher.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:${var.human_principal}"
}

# NOTE: no IAM binding of any kind grants the matcher access to splitsheet_eval.
# The absence is the control; it is asserted by an integration test, not assumed.

# ---------------------------------------------------------------------------
# Canonical snapshot (MusicBrainz 2026-07-17)
# ---------------------------------------------------------------------------

locals {
  canonical_manifest = jsondecode(file("${path.module}/../../docs/phase0/canonical_20260717_manifest.json"))
  canonical_prefix   = "raw/musicbrainz/canonical/snapshot_date=2026-07-17"
}

# External view of the landed snapshot. One explicit URI, no wildcard, so the table's
# contents cannot change because a file appeared under the prefix.
resource "google_bigquery_table" "ext_canonical_2026_07_17" {
  dataset_id          = google_bigquery_dataset.bronze.dataset_id
  table_id            = "ext_canonical_2026_07_17"
  project             = var.project_id
  deletion_protection = false

  description = "MusicBrainz canonical snapshot 2026-07-17, read in place from GCS. Measured grain: exactly one row per recording_mbid (31,554,198 rows, 31,554,198 distinct MBIDs, 0 duplicates)."

  external_data_configuration {
    # Explicitly false: the schema below is declared, so autodetect must not override it.
    autodetect    = false
    source_format = "CSV"
    # Required. Without it the provider defaults to NONE and BigQuery reads the gzip
    # bytes as text, failing with "line contains only 1 columns" at offsets that look
    # like garbage because they are compressed bytes.
    compression           = "GZIP"
    source_uris           = [local.canonical_manifest.landed_object]
    ignore_unknown_values = false

    csv_options {
      quote             = "\""
      skip_leading_rows = 1
    }

    # Schema is declared, not autodetected: an autodetect that guesses differently after a
    # snapshot refresh would silently change column types under the blocking index.
    schema = jsonencode([
      { name = "id", type = "INT64" },
      { name = "artist_credit_id", type = "INT64" },
      { name = "artist_mbids", type = "STRING" },
      { name = "artist_credit_name", type = "STRING" },
      { name = "release_mbid", type = "STRING" },
      { name = "release_name", type = "STRING" },
      { name = "recording_mbid", type = "STRING" },
      { name = "recording_name", type = "STRING" },
      { name = "combined_lookup", type = "STRING" },
      { name = "score", type = "INT64" },
    ])
  }
}

# Materialised canonical recordings.
#
# GRAIN: one row per recording_mbid, per snapshot_date. This is measured, not assumed --
# 31,554,198 rows against 31,554,198 distinct recording_mbid, zero repeats, zero exact
# duplicate rows, zero rows without an MBID. Had the file carried semantically distinct
# variants per recording, collapsing them here would have destroyed candidates silently,
# which is why the grain was profiled before this table was written.
resource "google_bigquery_table" "bronze_canonical_recordings" {
  dataset_id          = google_bigquery_dataset.bronze.dataset_id
  table_id            = "bronze_canonical_recordings"
  project             = var.project_id
  deletion_protection = true

  description = "One row per recording_mbid per snapshot_date. Grain measured, not assumed. Source: MusicBrainz canonical dump 2026-07-17, CC0."

  schema = jsonencode([
    { name = "snapshot_date", type = "DATE", mode = "REQUIRED", description = "Snapshot the row came from. Part of the grain." },
    { name = "recording_mbid", type = "STRING", mode = "REQUIRED", description = "MusicBrainz recording MBID. Unique within a snapshot." },
    { name = "recording_name", type = "STRING", mode = "REQUIRED", description = "Canonical recording title, 100% populated." },
    { name = "artist_credit_name", type = "STRING", mode = "REQUIRED", description = "Canonical artist credit, 100% populated." },
    { name = "artist_credit_id", type = "INT64", mode = "NULLABLE" },
    { name = "artist_mbids", type = "STRING", mode = "NULLABLE", description = "Comma-separated in the source; left as delivered." },
    { name = "release_mbid", type = "STRING", mode = "NULLABLE" },
    { name = "release_name", type = "STRING", mode = "NULLABLE" },
    { name = "combined_lookup", type = "STRING", mode = "NULLABLE", description = "MusicBrainz's own lookup key. Retained for comparison; NOT used to build our keys." },
    { name = "score", type = "INT64", mode = "NULLABLE", description = "Source popularity rank. Semantics not established; unused." },
    { name = "source_object", type = "STRING", mode = "REQUIRED" },
    { name = "ingestion_run_id", type = "STRING", mode = "REQUIRED" },
    { name = "ingested_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

# Blocking index, generated by src/normalization and loaded as data.
#
# GRAIN: recording_mbid + lookup_stage + lookup_key.
#
# A recording contributes one row per stage, because the exact stage must never be able to
# see a fallback key. Rows with an empty key are never emitted: a blank key would form a
# bucket that matches everything unmatchable.
resource "google_bigquery_table" "canonical_blocking_index" {
  dataset_id          = google_bigquery_dataset.bronze.dataset_id
  table_id            = "canonical_blocking_index"
  project             = var.project_id
  deletion_protection = true

  description = "Blocking keys per canonical recording. Grain: recording_mbid + lookup_stage + lookup_key. Keys produced by src/normalization only; the rules are never re-expressed in SQL."

  # Clustered, not partitioned: every blocking query filters on lookup_key, and clustering
  # is what makes that filter prune. This is the first clustering in the project and it is
  # justified by the access pattern, which is a single equality join on lookup_key.
  clustering = ["lookup_key"]

  schema = jsonencode([
    { name = "snapshot_date", type = "DATE", mode = "REQUIRED" },
    { name = "recording_mbid", type = "STRING", mode = "REQUIRED" },
    { name = "lookup_stage", type = "STRING", mode = "REQUIRED", description = "EXACT or FALLBACK. Part of the grain: a stage must only ever join against its own keys." },
    { name = "lookup_key", type = "STRING", mode = "REQUIRED", description = "Never empty by construction." },
    { name = "normalization_version", type = "STRING", mode = "REQUIRED" },
    { name = "ingestion_run_id", type = "STRING", mode = "REQUIRED" },
    { name = "ingested_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

# ---------------------------------------------------------------------------
# Silver layer and the normalization mapping
# ---------------------------------------------------------------------------

# Created now because Phase 3B genuinely produces silver output. It was deliberately
# absent until something needed it.
resource "google_bigquery_dataset" "silver" {
  dataset_id  = "splitsheet_silver"
  project     = var.project_id
  location    = var.region
  description = "Normalized listens and blocking candidates. Contains no user_id and no ListenBrainz-derived identifier."

  delete_contents_on_destroy = false

  labels = {
    project = "splitsheet"
    layer   = "silver"
    phase   = "3b"
  }
}

# Normalization output, one row per DISTINCT raw pair rather than per listen.
#
# 38,199,641 listens carry only 4,599,791 distinct pairs, so normalizing per pair is 8.3x
# less work and keeps the rules in a single Python implementation. The rules are never
# re-expressed in SQL; this table is how their output reaches BigQuery.
resource "google_bigquery_table" "listen_pair_normalization" {
  dataset_id          = google_bigquery_dataset.bronze.dataset_id
  table_id            = "listen_pair_normalization"
  project             = var.project_id
  deletion_protection = true

  description = "Normalization output per distinct (artist_name, recording_name) pair, produced by src/normalization. Join key: pair_hash = SHA256(artist || U+001F || recording)."

  clustering = ["pair_hash"]

  schema = jsonencode([
    { name = "pair_hash", type = "STRING", mode = "REQUIRED", description = "SHA-256 of the two raw strings joined by U+001F. An identity function over the inputs, not a normalization rule, so the same expression in SQL creates no second implementation." },
    { name = "artist_name", type = "STRING", mode = "REQUIRED" },
    { name = "recording_name", type = "STRING", mode = "REQUIRED" },
    { name = "artist_normalized_unicode", type = "STRING", mode = "REQUIRED", description = "Script-preserving normalized value. Not a lookup key." },
    { name = "recording_normalized_unicode", type = "STRING", mode = "REQUIRED" },
    { name = "lookup_exact", type = "STRING", mode = "REQUIRED", description = "Empty string when not AVAILABLE. Never join on an empty key." },
    { name = "lookup_fallback", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_status", type = "STRING", mode = "REQUIRED" },
    { name = "exact_key_status", type = "STRING", mode = "REQUIRED" },
    { name = "fallback_key_status", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_version", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_rules_sha256", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_run_id", type = "STRING", mode = "REQUIRED" },
  ])
}

# One row per listen, normalized.
#
# release_normalized_* is deliberately ABSENT. Nothing in Phase 3B blocks, joins or filters
# on release, so storing it would be carrying a column for a use that does not exist. The
# library still produces it; retaining it in memory is free, persisting it is not.
resource "google_bigquery_table" "silver_listens_normalized" {
  dataset_id          = google_bigquery_dataset.silver.dataset_id
  table_id            = "silver_listens_normalized"
  project             = var.project_id
  deletion_protection = true

  description = "Exactly one row per listen_hash for 2026-06: 38,199,641 rows. Raw strings plus normalization output and statuses. No user_id, no ListenBrainz-derived identifier, no reference label."

  time_partitioning {
    type  = "DAY"
    field = "listened_at"
  }
  require_partition_filter = true

  schema = jsonencode([
    { name = "listen_hash", type = "STRING", mode = "REQUIRED" },
    { name = "listened_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "artist_name", type = "STRING", mode = "REQUIRED" },
    { name = "recording_name", type = "STRING", mode = "REQUIRED" },
    { name = "release_name", type = "STRING", mode = "NULLABLE", description = "Raw, retained for provenance. Not normalized here: nothing in this phase uses it." },
    { name = "artist_normalized_unicode", type = "STRING", mode = "REQUIRED" },
    { name = "recording_normalized_unicode", type = "STRING", mode = "REQUIRED" },
    { name = "lookup_exact", type = "STRING", mode = "REQUIRED" },
    { name = "lookup_fallback", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_status", type = "STRING", mode = "REQUIRED" },
    { name = "exact_key_status", type = "STRING", mode = "REQUIRED" },
    { name = "fallback_key_status", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_version", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_rules_sha256", type = "STRING", mode = "REQUIRED" },
    { name = "ingestion_run_id", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_run_id", type = "STRING", mode = "REQUIRED" },
  ])
}

# --- matcher identity: read its inputs, write only silver -------------------------------
# Still no access of any kind to splitsheet_eval. The absence remains the control, and it
# is re-asserted by integration test after every IAM change here.

resource "google_bigquery_table_iam_member" "matcher_reads_blocking_index" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.bronze.dataset_id
  table_id   = google_bigquery_table.canonical_blocking_index.table_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.matcher.email}"
}

resource "google_bigquery_table_iam_member" "matcher_reads_pair_normalization" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.bronze.dataset_id
  table_id   = google_bigquery_table.listen_pair_normalization.table_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.matcher.email}"
}

# Write access is scoped to the silver dataset only. Not bronze, not eval, not project.
resource "google_bigquery_dataset_iam_member" "matcher_writes_silver" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.matcher.email}"
}

# Blocking candidates.
#
# GRAIN: listen_hash + candidate_recording_mbid + block_method + blocking_version +
#        candidate_run_id. Uniqueness on that tuple is asserted by integration test.
#
# This table is label-blind by construction: nothing that produces it can read
# splitsheet_eval, and no column here derives from a label.
resource "google_bigquery_table" "silver_match_candidates" {
  dataset_id          = google_bigquery_dataset.silver.dataset_id
  table_id            = "silver_match_candidates"
  project             = var.project_id
  deletion_protection = true

  description = "Blocking candidates only. No score, no threshold, no chosen match. A listen with several candidates has several rows and none of them is preferred."

  clustering = ["listen_hash"]

  schema = jsonencode([
    { name = "listen_hash", type = "STRING", mode = "REQUIRED" },
    { name = "candidate_recording_mbid", type = "STRING", mode = "REQUIRED" },
    { name = "block_method", type = "STRING", mode = "REQUIRED", description = "EXACT or FALLBACK. FALLBACK may only appear for a listen with zero EXACT candidates." },
    { name = "block_key", type = "STRING", mode = "REQUIRED", description = "The key that produced the candidate. Never empty." },
    { name = "canonical_snapshot_date", type = "DATE", mode = "REQUIRED" },
    { name = "blocking_version", type = "STRING", mode = "REQUIRED" },
    { name = "normalization_version", type = "STRING", mode = "REQUIRED" },
    { name = "candidate_run_id", type = "STRING", mode = "REQUIRED", description = "Deterministic from source + config, so the same inputs yield the same run id." },
    { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}
