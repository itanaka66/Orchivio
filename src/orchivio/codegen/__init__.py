"""Artifact generators derived from the model: the data API's OpenAPI document and a typed TypeScript client."""

from .openapi import build_openapi
from .typescript import build_client

__all__ = ["build_client", "build_openapi"]
