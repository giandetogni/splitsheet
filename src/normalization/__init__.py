"""Deterministic staged normalization. Pure: no I/O, no clock, no randomness, no GCP."""

from .normalizer import (
    KeyStatus,
    NormalizationStatus,
    NormalizedFields,
    aggressive,
    fold,
    normalize,
    normalized_unicode,
)
from .rules import NormalizationRules, load_rules

__all__ = ["KeyStatus", "NormalizationRules", "NormalizationStatus", "NormalizedFields",
           "aggressive", "fold", "load_rules", "normalize", "normalized_unicode"]
