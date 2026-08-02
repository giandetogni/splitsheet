"""Deterministic dev/holdout assignment for evaluation.

Pure and stateless: the bucket for a recording depends only on its MBID and the frozen
config, never on read order, partitioning or how many rows happened to arrive first.

This module is EVALUATION-only. It is never imported by normalization or by candidate
generation, and the identity that generates candidates has no access to the labels this
module is used to segment.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config/evaluation_split.yml"

DEV = "dev"
HOLDOUT = "holdout"


@dataclass(frozen=True)
class SplitConfig:
    split_version: str
    salt: str
    hex_prefix_chars: int
    modulus: int
    dev_upper_exclusive: int
    unlabelled_bucket: str
    label_absent_bucket: str

    def sql_expression(self, column: str = "mapper_recording_mbid") -> str:
        """The identical rule as BigQuery SQL, so Python and SQL cannot disagree.

        Derived from the same config values rather than written out by hand, which is what
        stops the two implementations drifting apart.
        """
        return (
            f"CASE WHEN {column} IS NULL THEN '{self.unlabelled_bucket}' "
            f"WHEN MOD(CAST(CONCAT('0x', SUBSTR(TO_HEX(SHA256(CONCAT("
            f"'{self.salt}', ':', {column}))), 1, {self.hex_prefix_chars})) AS INT64), "
            f"{self.modulus}) < {self.dev_upper_exclusive} THEN '{DEV}' "
            f"ELSE '{HOLDOUT}' END"
        )


def load_split_config(path: str | pathlib.Path | None = None) -> SplitConfig:
    import yaml

    p = pathlib.Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(p) as fh:
        raw = yaml.safe_load(fh)
    if raw["algorithm"] != "sha256_mod_100":
        raise ValueError(f"unsupported split algorithm: {raw['algorithm']!r}")
    dev, holdout = raw["buckets"]["dev"], raw["buckets"]["holdout"]
    if dev["lower_inclusive"] != 0 or dev["upper_exclusive"] != holdout["lower_inclusive"]:
        raise ValueError("dev and holdout buckets must be contiguous and start at 0")
    if holdout["upper_exclusive"] != raw["modulus"]:
        raise ValueError("buckets must cover the whole modulus")
    return SplitConfig(
        split_version=str(raw["split_version"]),
        salt=raw["salt"],
        hex_prefix_chars=int(raw["hex_prefix_chars"]),
        modulus=int(raw["modulus"]),
        dev_upper_exclusive=int(dev["upper_exclusive"]),
        unlabelled_bucket=raw["unlabelled_bucket"],
        label_absent_bucket=raw["label_absent_from_canonical_bucket"],
    )


def bucket_of(mapper_recording_mbid: str | None, cfg: SplitConfig) -> str:
    """Assign one recording to dev or holdout. Absent label is neither."""
    if not mapper_recording_mbid:
        return cfg.unlabelled_bucket
    digest = hashlib.sha256(f"{cfg.salt}:{mapper_recording_mbid}".encode()).hexdigest()
    value = int(digest[: cfg.hex_prefix_chars], 16) % cfg.modulus
    return DEV if value < cfg.dev_upper_exclusive else HOLDOUT
