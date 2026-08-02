"""Evaluation-only helpers. Never imported by normalization or candidate generation."""

from .split import DEV, HOLDOUT, SplitConfig, bucket_of, load_split_config

__all__ = ["DEV", "HOLDOUT", "SplitConfig", "bucket_of", "load_split_config"]
