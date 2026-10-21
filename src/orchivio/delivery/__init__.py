"""Environments, deployment packages and promotion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orchivio.integrations import SecretUnavailable, resolve_secret

from .migrate import run_contract, run_expand
from .package import (
    FORMAT,
    Package,
    PackageError,
    VerifiedPackage,
    build_package,
    generate_key,
    key_id,
    plan_migration,
    public_key_of,
    sbom,
    verify_package,
)

if TYPE_CHECKING:
    from orchivio.db.models import EnvironmentConstant


def resolve_constants(constants: list[EnvironmentConstant]) -> dict[str, Any]:
    """Resolve a list of EnvironmentConstants to a plain dict (FR-096).

    Plain-value constants are taken as-is. Secret references are resolved via
    ``resolve_secret``; constants whose secret is currently unavailable are omitted
    so the flow can still run if it does not access that key.
    """
    out: dict[str, Any] = {}
    for c in constants:
        if c.value is not None:
            out[c.key] = c.value
        elif c.secret_ref is not None:
            try:
                out[c.key] = resolve_secret(c.secret_ref)
            except SecretUnavailable:
                pass  # constant excluded; flows that reference it will raise at eval time
    return out


__all__ = ["FORMAT", "Package", "PackageError", "VerifiedPackage", "build_package", "generate_key", "key_id",
           "plan_migration", "public_key_of", "resolve_constants", "run_contract", "run_expand", "sbom", "verify_package"]
