"""Application model: schema, validation, diff, migration (see docs/MODEL-SPEC.md)."""

from .diff import Change, MissingElementId, ModelDiff, diff_models
from .ids import assign_element_ids, iter_elements
from .migrate import UnsupportedSchemaVersion, migrate_to_latest
from .schema import SCHEMA_VERSION, AppModel, canonical_json, model_hash
from .validate import Issue, ModelIndex, has_errors, validate_model

__all__ = [
    "SCHEMA_VERSION",
    "AppModel",
    "Change",
    "Issue",
    "MissingElementId",
    "ModelDiff",
    "ModelIndex",
    "UnsupportedSchemaVersion",
    "assign_element_ids",
    "canonical_json",
    "diff_models",
    "has_errors",
    "iter_elements",
    "migrate_to_latest",
    "model_hash",
    "validate_model",
]
