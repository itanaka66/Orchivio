"""Application model: schema, validation, diff, migration (see docs/MODEL-SPEC.md)."""

from .canonical import (
    CanonicalError,
    build_archive,
    from_files,
    is_canonical_path,
    read_archive,
    to_files,
)
from .diff import Change, MissingElementId, ModelDiff, diff_models
from .ids import assign_element_ids, iter_elements
from .lint import RULES, Finding, Rule, blocking, catalog_markdown, lint
from .merge import Conflict, MergeResult, flatten, merge, unflatten
from .migrate import UnsupportedSchemaVersion, migrate_to_latest
from .refs import Ref, build_refs, usages
from .schema import SCHEMA_VERSION, AppModel, canonical_json, model_hash
from .validate import Issue, ModelIndex, has_errors, validate_flow, validate_model

__all__ = [
    "RULES",
    "SCHEMA_VERSION",
    "AppModel",
    "CanonicalError",
    "Change",
    "Conflict",
    "Finding",
    "Issue",
    "MergeResult",
    "MissingElementId",
    "ModelDiff",
    "ModelIndex",
    "Ref",
    "Rule",
    "UnsupportedSchemaVersion",
    "assign_element_ids",
    "blocking",
    "build_archive",
    "build_refs",
    "catalog_markdown",
    "canonical_json",
    "diff_models",
    "flatten",
    "from_files",
    "is_canonical_path",
    "has_errors",
    "iter_elements",
    "lint",
    "merge",
    "migrate_to_latest",
    "model_hash",
    "read_archive",
    "to_files",
    "unflatten",
    "usages",
    "validate_flow",
    "validate_model",
]
