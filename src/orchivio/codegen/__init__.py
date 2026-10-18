"""Artifact generators derived from the model: the data API's OpenAPI document and typed clients (TypeScript, Python, C#)."""

from .csharp import build_csharp_client
from .openapi import build_openapi
from .python import build_python_client
from .typescript import build_client

# language -> (file extension, generator, media type)
CLIENT_LANGUAGES = {
    "typescript": ("ts", build_client, "text/typescript"),
    "python": ("py", build_python_client, "text/x-python"),
    "csharp": ("cs", build_csharp_client, "text/plain"),
}

__all__ = ["CLIENT_LANGUAGES", "build_client", "build_csharp_client", "build_openapi", "build_python_client"]
