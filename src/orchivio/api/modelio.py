"""Parsing untrusted model documents at the API boundary."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from orchivio.model import AppModel, UnsupportedSchemaVersion, migrate_to_latest

from .deps import api_error


def load_model(raw: dict[str, Any]) -> AppModel:
    """Migrate to the latest schema and validate. Errors never echo submitted values."""
    try:
        return AppModel.model_validate(migrate_to_latest(raw))
    except UnsupportedSchemaVersion as e:
        raise api_error(422, "unsupported_schema_version", str(e)) from e
    except ValidationError as e:
        details = [{"loc": list(x["loc"]), "message": x["msg"]} for x in e.errors()[:50]]
        raise api_error(422, "invalid_model", "model failed schema validation", details) from e
    except (KeyError, TypeError, AttributeError) as e:  # malformed 1.0 documents fail inside migration
        raise api_error(422, "invalid_model", "model has an unexpected structure") from e
