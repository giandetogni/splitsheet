"""Deterministic staged normalization. Pure: no I/O, no clock, no randomness, no GCP."""

from .normalizer import NormalizedFields, Status, aggressive, fold, normalize
from .rules import NormalizationRules, load_rules

__all__ = ["NormalizationRules", "NormalizedFields", "Status", "aggressive", "fold",
           "load_rules", "normalize"]
