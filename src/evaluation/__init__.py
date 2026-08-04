"""Evaluation-only helpers. Never imported by normalization or candidate generation."""

from .split import (
    CALIBRATION,
    DEV,
    HOLDOUT,
    VALIDATION,
    SplitConfig,
    bucket_of,
    load_split_config,
    partition_of,
)

__all__ = ["CALIBRATION", "DEV", "HOLDOUT", "VALIDATION", "SplitConfig", "bucket_of",
           "load_split_config", "partition_of"]
