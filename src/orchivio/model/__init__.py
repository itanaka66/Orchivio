"""Application model: schema, validation, diff, migration (see docs/MODEL-SPEC.md)."""

from .diff import Change, MissingElementId, ModelDiff, diff_models
from .ids import assign_element_ids, iter_elements
from .lint import RULES, Finding, Rule, blocking, catalog_markdown, lint
from .refs import Ref, build_refs, usages
from .migrate import UnsupportedSchemaVersion, migrate_to_latest
from .schema import SCHEMA_VERSION, AppModel, canonical_json, model_hash
from .validate import Issue, ModelIndex, has_errors, validate_model

__all__ = [
    "RULES",
    "SCHEMA_VERSION",
    "AppModel",
    "Change",
    "Finding",
    "Issue",
    "MissingElementId",
    "ModelDiff",
    "ModelIndex",
    "Ref",
    "Rule",
    "UnsupportedSchemaVersion",
    "assign_element_ids",
    "blocking",
    "build_refs",
    "catalog_markdown",
    "canonical_json",
    "diff_models",
    "has_errors",
    "iter_elements",
    "lint",
    "migrate_to_latest",
    "model_hash",
    "usages",
    "validate_model",
]
