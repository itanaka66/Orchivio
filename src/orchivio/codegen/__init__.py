"""Artifact generators derived from the model (OpenAPI today; typed clients are planned)."""

from .openapi import build_openapi

__all__ = ["build_openapi"]
