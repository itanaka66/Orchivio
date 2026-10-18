"""Consumed REST APIs: destinations (where) and OpenAPI import (what), docs/INTEGRATIONS.md, FR-081."""

from __future__ import annotations

import re
import uuid
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio.db import ModelInvalid, ProposalRepository, to_model
from orchivio.db.models import AppRevision, IntegrationDestination
from orchivio.db.proposals import NoChanges
from orchivio.integrations import (
    DestinationError,
    SecretUnavailable,
    check_url,
    destination_policy,
    resolve_secret,
)
from orchivio.integrations.dispatcher import SECRET_REF
from orchivio.integrations.openapi_import import ImportProblem, import_openapi
from orchivio.model import assign_element_ids

from ..deps import (
    Principal,
    api_error,
    audit,
    current_principal,
    get_db,
    load_application,
    not_found,
)
from .proposals import proposal_detail

router = APIRouter(tags=["integrations"])

NAME = r"^[a-z][a-z0-9-]{0,62}$"
KEY = r"^[a-z][a-z0-9_]{0,62}$"
HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
FORBIDDEN_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade", "te", "trailer",
                     "proxy-authorization", "cookie", "accept"}
MAX_SPEC_BYTES = 2 * 1024 * 1024


class Auth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(default="none", pattern="^(none|bearer|header)$")
    header: str | None = Field(default=None, max_length=64, description="header name for type=header, e.g. X-Api-Key")
    secret_ref: str | None = Field(default=None, max_length=300, description="env:NAME or file:/path holding the credential")


class DestinationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2000)
    auth: Auth = Field(default_factory=Auth)


def dest_out(d: IntegrationDestination) -> dict[str, Any]:
    try:
        resolve_secret(d.secret_ref) if d.secret_ref else None
        available = True
    except SecretUnavailable:
        available = False
    return {"id": str(d.id), "application_id": str(d.application_id), "name": d.name, "base_url": d.base_url,
            "auth": {"type": d.auth_type, "header": d.auth_header, "secret_ref": d.secret_ref}, "credential_available": available}


@router.put("/applications/{app_id}/destinations/{name}")
def put_destination(app_id: uuid.UUID, name: str, body: DestinationIn, request: Request,
                    principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create or replace where a logical destination of the model points. Admin only: it decides what the app may reach."""
    app, project = load_application(db, principal, app_id, "integration.manage")
    if not re.fullmatch(NAME, name):
        raise api_error(422, "invalid_name", "destination names are lower-case letters, digits and -")
    if "?" in body.base_url or "#" in body.base_url:
        raise api_error(422, "destination_refused", "the base URL must not contain a query string or fragment")
    try:
        check_url(body.base_url, destination_policy(request.app.state.settings))
    except DestinationError as e:
        raise api_error(422, "destination_refused", str(e)) from e
    a = body.auth
    if a.type == "none":
        if a.header or a.secret_ref:
            raise api_error(422, "invalid_auth", "type none takes no header or secret_ref")
    else:
        if not a.secret_ref or not SECRET_REF.match(a.secret_ref):
            raise api_error(422, "invalid_secret_ref", "give secret_ref as env:NAME or file:/path")
        try:
            resolve_secret(a.secret_ref)
        except SecretUnavailable as e:
            raise api_error(422, "secret_unavailable", str(e)) from e
        if a.type == "header" and (not a.header or not HEADER.match(a.header) or a.header.lower() in FORBIDDEN_HEADERS):
            raise api_error(422, "invalid_auth", "give a plain header name that is not one of the transport's own headers")
        if a.type == "bearer" and a.header:
            raise api_error(422, "invalid_auth", "bearer authentication always uses the Authorization header")
    row = db.scalar(select(IntegrationDestination).where(IntegrationDestination.application_id == app.id, IntegrationDestination.name == name))
    if row is None:
        row = IntegrationDestination(application_id=app.id, name=name, created_by=principal.user_id)
        db.add(row)
    row.base_url, row.auth_type, row.auth_header, row.secret_ref = body.base_url, a.type, a.header, a.secret_ref
    db.flush()
    audit(db, request, "destination.set", actor_id=principal.user_id, org_id=project.organization_id, target_type="destination",
          target_id=row.id, meta={"name": name, "auth": a.type})  # the URL may carry tokens: not logged
    db.commit()
    return dest_out(row)


@router.get("/applications/{app_id}/destinations")
def list_destinations(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "integration.manage")
    rows = db.scalars(select(IntegrationDestination).where(IntegrationDestination.application_id == app.id).order_by(IntegrationDestination.name))
    return {"items": [dest_out(d) for d in rows]}


@router.delete("/applications/{app_id}/destinations/{name}", status_code=204)
def delete_destination(app_id: uuid.UUID, name: str, request: Request, principal: Principal = Depends(current_principal),
                       db: Session = Depends(get_db)) -> Response:
    app, project = load_application(db, principal, app_id, "integration.manage")
    row = db.scalar(select(IntegrationDestination).where(IntegrationDestination.application_id == app.id, IntegrationDestination.name == name))
    if row is None:
        raise not_found("destination")
    audit(db, request, "destination.deleted", actor_id=principal.user_id, org_id=project.organization_id, target_type="destination",
          target_id=row.id, meta={"name": name})
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.post("/applications/{app_id}/integrations/openapi-import", status_code=201)
async def openapi_import(app_id: uuid.UUID, request: Request, key: str = Query(pattern=KEY), destination: str = Query(pattern=NAME),
                         module: str | None = Query(None, pattern=r"^[a-z][a-z0-9_]{0,62}$"),
                         principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Body: an OpenAPI 3.x document (JSON or YAML). The result is a change *proposal* adding the integration and the
    transient entities that type it; nothing changes until it is approved (it is an A3 change)."""
    app, project = load_application(db, principal, app_id, "integration.manage")
    if int(request.headers.get("content-length") or 0) > MAX_SPEC_BYTES:
        raise api_error(413, "spec_too_large", "the specification is limited to 2 MiB")
    raw = await request.body()
    if len(raw) > MAX_SPEC_BYTES:
        raise api_error(413, "spec_too_large", "the specification is limited to 2 MiB")
    if app.current_revision_id is None:
        raise api_error(409, "no_revision", "the application has no model yet")
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))  # JSON is valid YAML; safe_load builds plain data only
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise api_error(422, "invalid_spec", "the specification is not valid JSON or YAML") from e
    model = to_model(db.get(AppRevision, app.current_revision_id))
    mod = next((m for m in model.modules if m.key == module), None) if module else model.modules[0]
    if mod is None:
        raise api_error(422, "unknown_module", "the model has no such module")
    if any(i.key == key for i in mod.integrations or []):
        raise api_error(409, "integration_exists", f"module '{mod.key}' already has an integration '{key}'; choose another key")
    try:
        result = import_openapi(doc, key=key, destination=destination, taken_entities={e.name for e in mod.entities})
    except ImportProblem as e:
        raise api_error(422, "invalid_spec", str(e)) from e
    new = model.model_copy(deep=True)
    target = next(m for m in new.modules if m.key == mod.key)
    target.entities = [*target.entities, *result.entities]
    target.integrations = [*(target.integrations or []), result.integration]
    try:
        title = f"Import OpenAPI as integration {mod.key}.{key}"
        proposal = ProposalRepository(db).create(app.id, assign_element_ids(new, previous=model), source="human", title=title,
                                                 created_by=principal.user_id, organization_id=project.organization_id)
    except ModelInvalid as e:
        raise api_error(422, "import_invalid", "the imported definition fails model validation",
                        [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    except NoChanges as e:  # pragma: no cover - the import always adds an integration
        raise api_error(409, "no_changes", str(e)) from e
    audit(db, request, "integration.imported", actor_id=principal.user_id, org_id=project.organization_id, target_type="application",
          target_id=app.id, meta={"key": key, "operations": len(result.integration.operations), "entities": len(result.entities),
                                  "skipped": len(result.skipped)})
    db.commit()
    out = proposal_detail(proposal).model_dump(mode="json")
    out["import"] = {"integration": f"{mod.key}.{key}", "operations": [o.id for o in result.integration.operations],
                     "entities": [e.name for e in result.entities], "warnings": result.warnings, "skipped": result.skipped}
    return out
