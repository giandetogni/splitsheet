"""A reproducible content digest for a financial publication, defined exactly once.

WHY THIS EXISTS AS ITS OWN MODULE: "the publication is immutable" is only a claim until something
can recompute a fingerprint and compare it. Both the publisher and the baseline freeze need that
fingerprint, and they must compute it identically or the comparison proves nothing.

WHY IT IS TWO-LEVEL. The obvious form -- SHA256 over one ordered STRING_AGG of every row -- builds a
single ~600 MB string for 5.9M rows and fails with "Resources exceeded during query execution: peak
usage 119% of limit". Measured, not predicted: that is exactly how the first attempt died. So the
digest is computed like a small Merkle tree:

    per row      SHA-256 of the row's keys and amounts
    per bucket   SHA-256 of the row hashes in that bucket, ordered
    overall      SHA-256 of the bucket digests, ordered by bucket

Each bucket aggregates a few thousand hashes instead of millions. The result depends only on row
CONTENT -- not on row order, not on partition layout, not on how many slots BigQuery used -- so it
is reproducible across runs and across engines that implement SHA-256 the same way.

WHAT IS DELIBERATELY EXCLUDED: `published_at` and `publication_status`. The digest protects the
amounts and their keys; the timestamp is wall-clock metadata that is proven unchanged separately, and
folding it in would make the digest change for a reason that has nothing to do with the money.
"""

from __future__ import annotations

#: Bucket count. 512 buckets over ~5.9M rows is ~11.5k hashes per bucket: comfortably inside memory
#: while keeping the final aggregation trivial. Part of the digest definition -- changing it changes
#: every digest, so it is a constant here rather than a parameter.
DIGEST_BUCKETS = 512

#: The columns whose values the digest covers, in a fixed order.
DIGEST_COLUMNS = (
    "period",
    "recording_mbid",
    "rights_holder_id",
    "split_version_id",
    "rate_card_id",
    "holder_share_pct",
    "holder_payout",
    "gross_royalty",
)


def row_key_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    fields = ", ".join(f"{prefix}{c}" for c in DIGEST_COLUMNS)
    fmt = "|".join(["%t"] * len(DIGEST_COLUMNS))
    return f"FORMAT('{fmt}', {fields})"


def publication_digest_sql(fact_table: str, attribution_run_id: str) -> str:
    """SQL returning one row: content_digest, row_count, and the covered totals.

    Kept as a single statement so caller and callee cannot disagree about which rows were hashed.
    """
    return f"""
    WITH rows_hashed AS (
      SELECT TO_HEX(SHA256({row_key_sql()})) AS row_hash,
             holder_payout
      FROM `{fact_table}`
      WHERE attribution_run_id = '{attribution_run_id}'
    ),
    bucketed AS (
      SELECT MOD(ABS(FARM_FINGERPRINT(row_hash)), {DIGEST_BUCKETS}) AS bucket,
             TO_HEX(SHA256(STRING_AGG(row_hash, '' ORDER BY row_hash))) AS bucket_digest,
             COUNT(*) AS bucket_rows,
             SUM(holder_payout) AS bucket_paid
      FROM rows_hashed
      GROUP BY bucket
    )
    SELECT
      TO_HEX(SHA256(STRING_AGG(bucket_digest, '' ORDER BY bucket))) AS content_digest,
      SUM(bucket_rows) AS row_count,
      SUM(bucket_paid) AS total_holder_payout,
      COUNT(*) AS buckets_used
    FROM bucketed
    """
