"""Module and template registry (FR-126, docs/REGISTRY.md).

Publishing, importing and withdrawing need ``registry.publish`` (organization admins). Browsing is open to members of the publishing
organization, and to everyone signed in for packages published with ``visibility: instance``. Installing a module only ever creates
a proposal; creating an application from a template goes through the same checks as creating one from a model.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio import __version__, telemetry
from orchivio.db import ModelInvalid, ProposalRepository, RevisionRepository, to_model
from orchivio.db.models import (
    Application,
    AppRevision,
    Membership,
    Organization,
    RegistryEntry,
    RegistryInstall,
    RegistryRemote,
)
from orchivio.db.proposals import NoChanges
from orchivio.delivery.package import public_key_of
from orchivio.integrations import SecretUnavailable, destination_policy, resolve_secret
from orchivio.integrations.dispatcher import SECRET_REF
from orchivio.integrations.ssrf import DestinationError, check_url
from orchivio.model import assign_element_ids
from orchivio.registry import (
    RegistryError,
    Version,
    VersionError,
    build_package,
    merge_module,
    module_hash,
    remove_module,
    rename_module,
    resolve,
    satisfies,
    slice_module,
    template_model,
    verify_package,
)

from ..deps import (
    Principal,
    api_error,
    audit,
    authorize,
    current_principal,
    get_db,
    load_application,
    load_project,
    not_found,
    utcnow,
)
from ..schemas import Page
from .proposals import proposal_detail

router = APIRouter(tags=["registry"])

REF = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})/([a-z][a-z0-9-]{1,62})$")


class RequireIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(max_length=130, description="<organization-slug>/<name> of a module package this one builds on")
    range: str = Field(default="latest", max_length=64, description="the versions it works with: 1.2.3, ^1.2.0, ~1.2.0, >=1.2.0")


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    version: str = Field(max_length=64)
    kind: str = Field(default="module", pattern="^(module|template)$")
    module: str | None = Field(default=None, max_length=63, description="module key to publish (kind=module)")
    revision_id: uuid.UUID | None = None
    description: str = Field(default="", max_length=500)
    license: str = Field(default="", max_length=100)
    visibility: str = Field(default="org", pattern="^(org|instance)$")
    requires: list[RequireIn] = Field(default_factory=list, max_length=20, description="module packages this module needs (kind=module)")


class YankIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=300)


class InstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(max_length=130, description="<organization-slug>/<name>")
    version: str = Field(default="latest", max_length=64, description="1.2.3, ^1.2.0, ~1.2.0, >=1.2.0 or latest")
    overwrite_local_changes: bool = False
    as_module: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,62}$", description="install under another module key (first install only)")


class UninstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    overwrite_local_changes: bool = False


class RemoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=500, description="the other server's address, e.g. https://registry.example.com (without /api/v1)")
    token_ref: str | None = Field(default=None, max_length=300, description="env:NAME or file:/path holding an API token for that server")
    remote_organization_id: str | None = Field(default=None, max_length=64, description="the organization on that server that pushes go to")


class RemotePullIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(max_length=130, description="<organization-slug>/<name> on the remote server")
    version: str = Field(default="latest", max_length=64)
    visibility: str = Field(default="org", pattern="^(org|instance)$")


class RemotePushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(max_length=130, description="<organization-slug>/<name> of one of this organization's packages")
    version: str = Field(max_length=64)


class InstantiateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(max_length=130)
    version: str = Field(default="latest", max_length=64)
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    name: str = Field(min_length=1, max_length=200)


def _core(version: str) -> tuple[int, int, int]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _fail(e: RegistryError, status: int = 422):
    return api_error(status, e.code, e.message, e.details)


def _trusted(request: Request) -> list[str]:
    s = request.app.state.settings
    keys = list(s.package_trusted_public_keys)
    if s.package_signing_key is not None:
        keys.append(public_key_of(s.package_signing_key.get_secret_value()))
    return keys


def entry_out(e: RegistryEntry, slug: str) -> dict[str, Any]:
    m = e.manifest
    return {"id": str(e.id), "package": f"{slug}/{e.name}", "name": e.name, "organization": slug, "version": e.version, "kind": e.kind,
            "description": e.description, "license": e.license, "visibility": e.visibility, "digest": e.digest, "key_id": e.key_id,
            "imported": e.imported, "yanked": e.yanked_at is not None, "yanked_reason": e.yanked_reason, "contents": m.get("contents", {}),
            "model_hash": m.get("model_hash"), "min_runtime_version": m.get("min_runtime_version"), "created_at": e.created_at.isoformat()}


def _member_orgs(db: Session, principal: Principal) -> set[uuid.UUID]:
    if principal.token is not None:
        return {principal.token.organization_id}
    return set(db.scalars(select(Membership.organization_id).where(Membership.user_id == principal.user_id)))


def _readable(db: Session, principal: Principal, e: RegistryEntry, mine: set[uuid.UUID] | None = None) -> bool:
    return e.visibility == "instance" or e.organization_id in (mine if mine is not None else _member_orgs(db, principal))


def _find(db: Session, principal: Principal, ref: str, spec: str, *, kind: str | None = None) -> tuple[RegistryEntry, str]:
    """The entry ``ref`` at the highest version matching ``spec`` that the caller may read and that is not withdrawn."""
    m = REF.match(ref)
    if not m:
        raise api_error(422, "invalid_package", "use <organization-slug>/<name>")
    org = db.scalar(select(Organization).where(Organization.slug == m.group(1)))
    rows = list(db.scalars(select(RegistryEntry).where(RegistryEntry.organization_id == org.id, RegistryEntry.name == m.group(2)))) if org else []
    mine = _member_orgs(db, principal)
    rows = [r for r in rows if _readable(db, principal, r, mine)]
    if not rows:
        raise not_found("package")
    if kind is not None and rows[0].kind != kind:
        raise api_error(422, "wrong_kind", f"'{ref}' is a {rows[0].kind}, not a {kind}")
    live = {r.version: r for r in rows if r.yanked_at is None}
    spec = spec.strip()
    try:
        picked = (str(Version.parse(spec)) if spec[:1].isdigit() else resolve(spec, list(live)))
    except VersionError as e:
        raise api_error(422, "invalid_version", str(e)) from e
    if picked not in live:
        picked = None
    if picked is None:
        raise api_error(404, "no_matching_version", f"no available version of '{ref}' matches '{spec}'", {"available": sorted(live)})
    return live[picked], org.slug  # type: ignore[union-attr]


def _verified(request: Request, entry: RegistryEntry):
    try:
        data = request.app.state.artifacts.get(entry.artifact_key)
    except (FileNotFoundError, KeyError, ValueError) as e:
        raise api_error(409, "package_corrupt", "the package file is missing from the artifact store") from e
    try:
        v = verify_package(data, _trusted(request))
    except RegistryError as e:
        raise api_error(409, e.code, e.message) from e
    if v.digest != entry.digest:
        raise api_error(409, "package_corrupt", "the package file does not match its recorded digest")
    if _core(v.manifest.get("min_runtime_version", "0.0.0")) > _core(__version__):
        raise api_error(409, "runtime_too_old", f"this package needs Orchivio {v.manifest['min_runtime_version']} or newer")
    return v


# --- publishing ------------------------------------------------------------------------------------------------------


@router.post("/applications/{app_id}/registry/publish", status_code=201)
def publish(app_id: uuid.UUID, body: PublishIn, request: Request, principal: Principal = Depends(current_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    """Cut a module (or the whole application as a template) out of a revision, sign it and store this version for good."""
    app, project = load_application(db, principal, app_id, "registry.publish")
    key = request.app.state.settings.package_signing_key
    if key is None:
        raise api_error(503, "signing_not_configured", "set ORCHIVIO_PACKAGE_SIGNING_KEY (see `orchivio keygen`) to publish")
    rev_id = body.revision_id or app.current_revision_id
    rev = db.get(AppRevision, rev_id) if rev_id else None
    if rev is None or rev.application_id != app.id:
        raise api_error(409, "no_revision", "the application has no such revision")
    model = to_model(rev)
    requires: list[dict[str, str]] = []
    if body.requires and body.kind != "module":
        raise api_error(422, "requires_not_supported", "only modules can require other packages")
    own_slug = db.get(Organization, project.organization_id).slug  # type: ignore[union-attr]
    dep_keys: list[str] = []
    for req in body.requires:
        entry_dep, dep_slug = _find(db, principal, req.package, req.range, kind="module")
        dep_ref = f"{dep_slug}/{entry_dep.name}"
        if dep_ref == f"{own_slug}/{body.name}" or any(r["package"] == dep_ref for r in requires):
            raise api_error(422, "invalid_requires", f"'{dep_ref}' is listed twice or is the package itself")
        dep_module = (entry_dep.manifest.get("contents", {}).get("modules") or [None])[0]
        if dep_module is None or all(m.key != dep_module for m in model.modules):
            raise api_error(422, "dependency_not_in_model", f"module '{dep_module}' of '{dep_ref}' is not part of this revision, so the dependency cannot be checked")
        requires.append({"package": dep_ref, "range": req.range, "module": dep_module})
        dep_keys.append(dep_module)
        # what the dependency itself builds on must be present too, or its own references would not validate here
        for sub in entry_dep.manifest.get("requires", []):
            if sub.get("module") and sub["module"] not in dep_keys:
                dep_keys.append(sub["module"])
    for k in dep_keys:
        if all(m.key != k for m in model.modules):
            raise api_error(422, "dependency_not_in_model", f"module '{k}' (needed by a dependency) is not part of this revision, so the dependency cannot be checked")
    try:
        if body.kind == "module":
            if not body.module:
                raise api_error(422, "module_required", "give the key of the module to publish")
            model = slice_module(model, body.module, name=body.name, depends_on=dep_keys)
        pkg = build_package(model, name=body.name, version=body.version, kind=body.kind, description=body.description, license=body.license,
                            runtime_version=".".join(map(str, _core(__version__))), signing_seed=key.get_secret_value(), requires=requires)
    except RegistryError as e:
        raise _fail(e) from e
    org_id = project.organization_id
    earlier = list(db.scalars(select(RegistryEntry).where(RegistryEntry.organization_id == org_id, RegistryEntry.name == body.name)))
    if earlier and earlier[0].kind != body.kind:
        raise api_error(409, "kind_mismatch", f"'{body.name}' is already published as a {earlier[0].kind}")
    entry = RegistryEntry(organization_id=org_id, name=body.name, version=str(Version.parse(body.version)), kind=body.kind, description=body.description,
                          license=body.license, visibility=body.visibility, digest=pkg.digest, key_id=pkg.key_id, artifact_key=f"registry/{pkg.digest}.zip",
                          manifest=pkg.manifest, published_by=principal.user_id)
    db.add(entry)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "version_exists", f"{body.name} {body.version} is already published; versions are immutable, publish a new one") from e
    request.app.state.artifacts.put(entry.artifact_key, pkg.data)
    slug = own_slug
    audit(db, request, "registry.published", actor_id=principal.user_id, org_id=org_id, target_type="registry_entry", target_id=entry.id,
          meta={"name": body.name, "version": entry.version, "kind": body.kind, "visibility": body.visibility, "digest": pkg.digest})
    db.commit()
    return entry_out(entry, slug)


@router.post("/organizations/{org_id}/registry/import", status_code=201)
async def import_package(org_id: uuid.UUID, request: Request, visibility: str = Query("org", pattern="^(org|instance)$"),
                         principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Add a package built elsewhere (raw ``application/zip`` body). It must be signed by a key this server trusts."""
    limit = request.app.state.settings.max_import_bytes
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise api_error(413, "payload_too_large", "package too large")
        chunks.append(chunk)
    data = b"".join(chunks)

    def work() -> dict[str, Any]:
        authorize(db, principal, org_id, "registry.publish")
        return _store(db, request, principal, org_id, data, visibility, action="registry.imported")

    return await run_in_threadpool(work)


def _store(db: Session, request: Request, principal: Principal, org_id: uuid.UUID, data: bytes, visibility: str, *, action: str,
           extra_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify a package built elsewhere against the trusted keys and add it to this organization's registry."""
    try:
        v = verify_package(data, _trusted(request))
    except RegistryError as e:
        raise api_error(422, e.code, e.message) from e
    m = v.manifest
    if _core(m.get("min_runtime_version", "0.0.0")) > _core(__version__):
        raise api_error(409, "runtime_too_old", f"this package needs Orchivio {m['min_runtime_version']} or newer")
    earlier = db.scalar(select(RegistryEntry.kind).where(RegistryEntry.organization_id == org_id, RegistryEntry.name == m["name"]).limit(1))
    if earlier and earlier != m["kind"]:
        raise api_error(409, "kind_mismatch", f"'{m['name']}' is already published as a {earlier}")
    entry = RegistryEntry(organization_id=org_id, name=m["name"], version=str(Version.parse(m["version"])), kind=m["kind"], description=m.get("description", ""),
                          license=m.get("license", ""), visibility=visibility, digest=v.digest, key_id=m["key_id"], artifact_key=f"registry/{v.digest}.zip",
                          manifest=m, imported=True, published_by=principal.user_id)
    db.add(entry)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "version_exists", "this package version is already in the registry") from e
    request.app.state.artifacts.put(entry.artifact_key, data)
    audit(db, request, action, actor_id=principal.user_id, org_id=org_id, target_type="registry_entry", target_id=entry.id,
          meta={"name": entry.name, "version": entry.version, "key_id": entry.key_id, "digest": v.digest, **(extra_meta or {})})
    db.commit()
    return entry_out(entry, db.get(Organization, org_id).slug)  # type: ignore[union-attr]


def _publisher_entry(db: Session, principal: Principal, slug: str, name: str, version: str) -> tuple[RegistryEntry, Organization]:
    org = db.scalar(select(Organization).where(Organization.slug == slug))
    if org is None:
        raise not_found("package")
    authorize(db, principal, org.id, "registry.publish")
    e = db.scalar(select(RegistryEntry).where(RegistryEntry.organization_id == org.id, RegistryEntry.name == name, RegistryEntry.version == version))
    if e is None:
        raise not_found("package version")
    return e, org


@router.post("/registry/packages/{slug}/{name}/{version}/yank")
def yank(slug: str, name: str, version: str, body: YankIn, request: Request, principal: Principal = Depends(current_principal),
         db: Session = Depends(get_db)) -> dict[str, Any]:
    """Withdraw a version: it can no longer be installed or chosen by a range. Installed copies keep working."""
    e, org = _publisher_entry(db, principal, slug, name, version)
    e.yanked_at, e.yanked_reason = utcnow(), body.reason
    audit(db, request, "registry.yanked", actor_id=principal.user_id, org_id=org.id, target_type="registry_entry", target_id=e.id,
          meta={"name": name, "version": version, "reason": body.reason})
    db.commit()
    return entry_out(e, slug)


@router.post("/registry/packages/{slug}/{name}/{version}/unyank")
def unyank(slug: str, name: str, version: str, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    e, org = _publisher_entry(db, principal, slug, name, version)
    e.yanked_at, e.yanked_reason = None, None
    audit(db, request, "registry.unyanked", actor_id=principal.user_id, org_id=org.id, target_type="registry_entry", target_id=e.id, meta={"name": name, "version": version})
    db.commit()
    return entry_out(e, slug)


# --- browsing ----------------------------------------------------------------------------------------------------------


@router.get("/registry/packages", response_model=Page[dict])
def search(q: str | None = Query(None, max_length=100), kind: str | None = Query(None, pattern="^(module|template)$"),
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> Page[Any]:
    """Packages the caller can see, one row per package with its newest available version."""
    mine = _member_orgs(db, principal)
    stmt = select(RegistryEntry, Organization.slug).join(Organization, Organization.id == RegistryEntry.organization_id)
    if kind:
        stmt = stmt.where(RegistryEntry.kind == kind)
    groups: dict[tuple[str, str], list[tuple[RegistryEntry, str]]] = {}
    for e, slug in db.execute(stmt.limit(5000)):
        if _readable(db, principal, e, mine):
            groups.setdefault((slug, e.name), []).append((e, slug))
    needle = (q or "").casefold()
    rows = []
    for (slug, name), versions in sorted(groups.items()):
        live = [(e, s) for e, s in versions if e.yanked_at is None]
        if not live:
            continue
        releases = [es for es in live if not _sortkey(es[0].version).is_prerelease] or live
        best = max(releases, key=lambda es: _sortkey(es[0].version))[0]
        if needle and needle not in name and needle not in best.description.casefold():
            continue
        rows.append({**entry_out(best, slug), "versions": len(live)})
    return Page(items=rows[offset:offset + limit], limit=limit, offset=offset, total=len(rows))


def _sortkey(v: str) -> Version:
    return Version.parse(v)


@router.get("/registry/packages/{slug}/{name}")
def package_versions(slug: str, name: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    org = db.scalar(select(Organization).where(Organization.slug == slug))
    rows = list(db.scalars(select(RegistryEntry).where(RegistryEntry.organization_id == org.id, RegistryEntry.name == name))) if org else []
    mine = _member_orgs(db, principal)
    rows = sorted((r for r in rows if _readable(db, principal, r, mine)), key=lambda r: _sortkey(r.version), reverse=True)
    if not rows:
        raise not_found("package")
    live = [r for r in rows if r.yanked_at is None]
    releases = [r for r in live if not _sortkey(r.version).is_prerelease]
    return {"package": f"{slug}/{name}", "kind": rows[0].kind, "latest": (releases or live)[0].version if live else None, "versions": [entry_out(r, slug) for r in rows]}


@router.get("/registry/packages/{slug}/{name}/{version}/download")
def download(slug: str, name: str, version: str, request: Request, principal: Principal = Depends(current_principal),
             db: Session = Depends(get_db)) -> Response:
    """The signed package file, to import into another registry. Yanked versions can still be downloaded by the publisher."""
    org = db.scalar(select(Organization).where(Organization.slug == slug))
    e = db.scalar(select(RegistryEntry).where(RegistryEntry.organization_id == org.id, RegistryEntry.name == name, RegistryEntry.version == version)) if org else None
    if e is None or not _readable(db, principal, e):
        raise not_found("package version")
    if e.yanked_at is not None and e.organization_id not in _member_orgs(db, principal):
        raise not_found("package version")
    data = request.app.state.artifacts.get(e.artifact_key)
    audit(db, request, "registry.downloaded", actor_id=principal.user_id, org_id=e.organization_id, target_type="registry_entry", target_id=e.id, meta={"name": name, "version": version})
    db.commit()
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{slug}-{name}-{version}.orchivio-pkg.zip"'})


# --- using packages --------------------------------------------------------------------------------------------------


def _current(db: Session, app: Application):
    rev = db.get(AppRevision, app.current_revision_id) if app.current_revision_id else None
    if rev is None:
        raise api_error(409, "no_revision", "the application has no model yet")
    return to_model(rev)


def _history(db: Session, app_id: uuid.UUID) -> dict[str, list[RegistryInstall]]:
    """The application's registry events per module key, newest first."""
    out: dict[str, list[RegistryInstall]] = {}
    for r in db.scalars(select(RegistryInstall).where(RegistryInstall.application_id == app_id).order_by(RegistryInstall.created_at.desc(), RegistryInstall.id)):
        out.setdefault(r.module_key, []).append(r)
    return out


def _applied(model, rows: list[RegistryInstall]) -> RegistryInstall | None:
    """The install event whose module is what the model has right now (None if the module is absent or was changed since)."""
    if not rows:
        return None
    current = module_hash(model, rows[0].module_key)
    matching = [r for r in rows if r.action == "install" and current is not None and r.module_hash == current]
    return max(matching, key=lambda r: (Version.parse(r.version), r.created_at)) if matching else None


def _entry_for(db: Session, ref: str, version: str) -> RegistryEntry | None:
    m = REF.match(ref)
    org = db.scalar(select(Organization).where(Organization.slug == m.group(1))) if m else None
    return db.scalar(select(RegistryEntry).where(RegistryEntry.organization_id == org.id, RegistryEntry.name == m.group(2), RegistryEntry.version == version)) if org and m else None


def _requires_of(db: Session, row: RegistryInstall) -> list[dict[str, str]]:
    entry = _entry_for(db, row.package_ref, row.version)
    return list(entry.manifest.get("requires", [])) if entry else []


def _resolve(request: Request, db: Session, principal: Principal, ref: str, spec: str):
    """The install order (dependencies first) for ``ref`` at ``spec``: ``(chosen, order, root_ref)``. One version per package; a second request
    that the chosen version does not satisfy, or a cycle, is a conflict (no backtracking)."""
    chosen: dict[str, tuple[RegistryEntry, Any, str | None, list[str]]] = {}
    order: list[str] = []
    visiting: list[str] = []

    def visit(pref: str, pspec: str, parent: str | None) -> str:
        entry, slug = _find(db, principal, pref, pspec, kind="module")
        key = f"{slug}/{entry.name}"
        if key in chosen:
            if not satisfies(Version.parse(chosen[key][0].version), pspec if pspec[:1] in "^~>*l" else pspec):
                raise api_error(409, "dependency_conflict", f"'{key}' is needed at {pspec} by '{parent}' but {chosen[key][0].version} was already selected",
                                {"package": key, "selected": chosen[key][0].version, "wanted": pspec, "by": parent})
            chosen[key][3].append(pspec)
            return key
        if key in visiting:
            raise api_error(409, "dependency_cycle", "the packages depend on each other: " + " -> ".join([*visiting, key]))
        visiting.append(key)
        verified = _verified(request, entry)
        for req in verified.manifest.get("requires", []):
            visit(req["package"], req.get("range", "latest"), key)
        visiting.pop()
        chosen[key] = (entry, verified, parent, [pspec])
        order.append(key)
        return key

    root = visit(ref, spec, None)
    return chosen, order, root


@router.post("/applications/{app_id}/registry/install", status_code=201)
def install(app_id: uuid.UUID, body: InstallIn, request: Request, principal: Principal = Depends(current_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    """Add (or upgrade) a module and the modules it requires as one *proposal*. Roles and access rules they need appear in the diff."""
    with telemetry.span("registry.install", package=body.package):
        return _install(app_id, body, request, principal, db)


def _install(app_id: uuid.UUID, body: InstallIn, request: Request, principal: Principal, db: Session) -> dict[str, Any]:
    app, project = load_application(db, principal, app_id, "model.propose")
    chosen, order, root = _resolve(request, db, principal, body.package, body.version)
    target = _current(db, app)
    history = _history(db, app.id)
    model = target
    steps: list[dict[str, Any]] = []
    new_rows: list[RegistryInstall] = []
    for ref in order:
        entry, v, parent, specs = chosen[ref]
        is_root = ref == root
        source_key = v.model.modules[0].key
        present = {m.key for m in model.modules}
        known = next((r.module_key for rows in history.values() for r in rows if r.package_ref == ref and r.action == "install" and r.module_key in present), None)
        if is_root and body.as_module and known and body.as_module != known:
            raise api_error(409, "module_renamed", f"'{ref}' is already installed as module '{known}'; upgrades keep that key")
        key = known or (body.as_module if is_root and body.as_module else source_key)
        package_model = v.model
        if key != source_key:
            try:
                package_model = rename_module(v.model, key)
            except RegistryError as e:
                raise _fail(e) from e
        exists = key in present
        replace = False
        if exists:
            rows = history.get(key, [])
            if not any(r.package_ref == ref and r.action == "install" for r in rows):
                raise api_error(409, "module_exists" if is_root else "dependency_conflict",
                                f"the application already has a module '{key}' that did not come from '{ref}'" + ("" if is_root else f", which '{parent}' requires"))
            applied = _applied(model, rows)
            if applied is not None and not is_root and all(satisfies(Version.parse(applied.version), sp) for sp in specs):
                steps.append({"package": ref, "version": applied.version, "module": key, "action": "kept", "dependency": True})
                continue
            if applied is None and not body.overwrite_local_changes:
                raise api_error(409, "module_modified", f"module '{key}' was changed after it was installed; pass overwrite_local_changes to replace those changes")
            replace = True
        try:
            model = merge_module(model, package_model, replace=replace)
        except RegistryError as e:
            raise _fail(e, 409) from e
        new_rows.append(RegistryInstall(application_id=app.id, package_ref=ref, created_at=utcnow() + timedelta(microseconds=len(new_rows)), version=entry.version, module_key=key, source_key="" if key == source_key else source_key,
                                        action="install", dependency_of=None if is_root else parent, module_hash=module_hash(model, key) or "", installed_by=principal.user_id))
        steps.append({"package": ref, "version": entry.version, "module": key, "action": "upgrade" if replace else "install", "dependency": not is_root,
                      "renamed_from": source_key if key != source_key else None})
    added_roles = [r for r in model.security.roles if r not in target.security.roles]
    root_entry = chosen[root][0]
    try:
        proposal = ProposalRepository(db).create(app.id, assign_element_ids(model, previous=target), source="human",
                                                 title=f"{'Upgrade' if any(x['action'] == 'upgrade' and not x['dependency'] for x in steps) else 'Install'} module {root}@{root_entry.version}"
                                                       + (f" (+{sum(1 for x in steps if x['dependency'] and x['action'] != 'kept')} required)" if any(x["dependency"] and x["action"] != "kept" for x in steps) else ""),
                                                 created_by=principal.user_id, organization_id=project.organization_id)
    except ModelInvalid as e:
        raise api_error(422, "model_invalid", "the module does not fit this application", [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    except NoChanges as e:
        raise api_error(409, "already_installed", "this version is already part of the application") from e
    for r in new_rows:
        r.proposal_id = proposal.id
        db.add(r)
    audit(db, request, "registry.install_proposed", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"package": root, "version": root_entry.version, "steps": steps, "added_roles": added_roles, "proposal_id": str(proposal.id)})
    db.commit()
    main = next(x for x in steps if x["package"] == root)
    return {"proposal": proposal_detail(proposal).model_dump(mode="json"), "package": root, "version": root_entry.version, "module": main["module"],
            "upgrade": main["action"] == "upgrade", "added_roles": added_roles, "steps": steps}


@router.post("/applications/{app_id}/registry/uninstall", status_code=201)
def uninstall(app_id: uuid.UUID, body: UninstallIn, request: Request, principal: Principal = Depends(current_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    """Propose removing a module that came from the registry, together with its access rules and page access. Roles stay. Data tables of a deployed
    environment are not dropped by a revision; only the application model changes."""
    app, project = load_application(db, principal, app_id, "model.propose")
    target = _current(db, app)
    history = _history(db, app.id)
    rows = history.get(body.module, [])
    if not rows or rows[0].action != "install" and not any(r.action == "install" for r in rows):
        raise api_error(404, "not_installed", f"module '{body.module}' was not installed from the registry")
    if all(m.key != body.module for m in target.modules):
        raise api_error(409, "module_absent", f"the application has no module '{body.module}'")
    ref = next(r.package_ref for r in rows if r.action == "install")
    if _applied(target, rows) is None and not body.overwrite_local_changes:
        raise api_error(409, "module_modified", f"module '{body.module}' was changed after it was installed; pass overwrite_local_changes to remove it anyway")
    needed_by = []
    for key, other in history.items():
        if key == body.module or all(m.key != key for m in target.modules):
            continue
        latest = next((r for r in other if r.action == "install"), None)
        if latest and other[0].action == "install" and any(q.get("package") == ref for q in _requires_of(db, latest)):
            needed_by.append({"module": key, "package": latest.package_ref})
    if needed_by:
        raise api_error(409, "required_by", f"module '{body.module}' is required by other installed modules", {"required_by": needed_by})
    try:
        proposal = ProposalRepository(db).create(app.id, assign_element_ids(remove_module(target, body.module), previous=target), source="human",
                                                 title=f"Remove module {body.module} ({ref})", created_by=principal.user_id, organization_id=project.organization_id)
    except RegistryError as e:
        raise _fail(e, 409) from e
    except ModelInvalid as e:
        raise api_error(422, "model_invalid", "something else in the application still uses this module", [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    except NoChanges as e:
        raise api_error(409, "no_changes", str(e)) from e
    first = rows[0]
    db.add(RegistryInstall(application_id=app.id, package_ref=ref, created_at=utcnow(), version=first.version, module_key=body.module, source_key=first.source_key, action="uninstall",
                           module_hash=module_hash(target, body.module) or "", proposal_id=proposal.id, installed_by=principal.user_id))
    audit(db, request, "registry.uninstall_proposed", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"module": body.module, "package": ref, "proposal_id": str(proposal.id)})
    db.commit()
    return {"proposal": proposal_detail(proposal).model_dump(mode="json"), "module": body.module, "package": ref}


@router.get("/applications/{app_id}/registry/installs")
def installs(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Modules installed from the registry: state, version, what they require and what requires them, and whether a newer version exists."""
    app, _ = load_application(db, principal, app_id, "model.read")
    model = _current(db, app) if app.current_revision_id else None
    history = _history(db, app.id)
    out = []
    for key, rows in history.items():
        present = model is not None and any(m.key == key for m in model.modules)
        if rows[0].action == "uninstall" and not present:
            continue
        installs_ = [r for r in rows if r.action == "install"]
        if not installs_:
            continue
        applied = _applied(model, rows) if model is not None else None
        r = applied or installs_[0]
        state = "removal_pending" if rows[0].action == "uninstall" else ("not_applied" if not present else "installed" if applied else "modified")
        try:
            newest = _find(db, principal, r.package_ref, "latest")[0].version
        except Exception:  # noqa: BLE001 - the package may be gone or no longer visible; that is not an error here
            newest = None
        out.append({"module": key, "package": r.package_ref, "version": r.version, "state": state, "proposal_id": str(r.proposal_id) if r.proposal_id else None,
                    "latest_version": newest, "update_available": bool(newest and Version.parse(newest) > Version.parse(r.version)),
                    "renamed_from": r.source_key or None, "dependency_of": r.dependency_of, "requires": _requires_of(db, r)})
    by_ref = {x["package"]: x for x in out}
    for x in out:
        x["required_by"] = [y["module"] for y in out if any(q.get("package") == x["package"] for q in y["requires"])]
    del by_ref
    return {"items": out}


@router.get("/applications/{app_id}/registry/updates")
def registry_updates(app_id: uuid.UUID, principal: Principal = Depends(current_principal),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """List installed modules with available registry upgrades (FR-126)."""
    app, _ = load_application(db, principal, app_id, "model.read")
    model = _current(db, app) if app.current_revision_id else None
    history = _history(db, app.id)
    items = []
    for key, rows in history.items():
        present = model is not None and any(m.key == key for m in model.modules)
        if rows[0].action == "uninstall" and not present:
            continue
        installs_ = [r for r in rows if r.action == "install"]
        if not installs_:
            continue
        applied = _applied(model, rows) if model is not None else None
        r = applied or installs_[0]
        locally_modified = present and applied is None
        try:
            latest_entry, _ = _find(db, principal, r.package_ref, "latest")
            newest = latest_entry.version
        except Exception:  # noqa: BLE001
            newest = r.version
        up_to_date = Version.parse(newest) <= Version.parse(r.version)
        items.append({"module_key": key, "package": r.package_ref, "installed_version": r.version,
                      "latest_version": newest, "up_to_date": up_to_date, "locally_modified": locally_modified})
    return {"items": items}


class UpgradeAllIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = False


@router.post("/applications/{app_id}/registry/upgrade-all", status_code=201)
def upgrade_all(app_id: uuid.UUID, body: UpgradeAllIn, request: Request,
                principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Upgrade all non-locally-modified modules to their latest registry versions as one proposal (FR-126)."""
    app, project = load_application(db, principal, app_id, "model.propose")
    model = _current(db, app)
    history = _history(db, app.id)
    upgraded: list[str] = []
    skipped: list[str] = []
    target = model
    new_rows: list[RegistryInstall] = []
    for key, rows in history.items():
        present = any(m.key == key for m in target.modules)
        if rows[0].action == "uninstall" and not present:
            continue
        installs_ = [r for r in rows if r.action == "install"]
        if not installs_:
            continue
        applied = _applied(target, rows)
        r = applied or installs_[0]
        if applied is None:  # locally modified
            skipped.append(key)
            continue
        try:
            latest_entry, _ = _find(db, principal, r.package_ref, "latest")
        except Exception:  # noqa: BLE001
            skipped.append(key)
            continue
        if Version.parse(latest_entry.version) <= Version.parse(r.version):
            continue
        v = _verified(request, latest_entry)
        target = merge_module(target, v.model, replace=True)
        new_rows.append(RegistryInstall(application_id=app.id, package_ref=r.package_ref, created_at=utcnow(),
                                        version=latest_entry.version, module_key=key, source_key=r.source_key,
                                        action="install", module_hash=module_hash(target, key)))
        upgraded.append(key)
    if not upgraded or body.dry_run:
        return {"upgraded": upgraded, "skipped": skipped, "proposal_id": None, "dry_run": True}
    try:
        proposal = ProposalRepository(db).create(app.id, assign_element_ids(target, previous=model), source="human",
                                                 created_by=principal.user_id, organization_id=project.organization_id)
    except ModelInvalid as e:
        raise api_error(422, "model_invalid", "upgrade produced an invalid model",
                        [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    for row in new_rows:
        row.proposal_id = proposal.id
        db.add(row)
    audit(db, request, "registry.upgrade_all", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="application", target_id=app.id, meta={"upgraded": upgraded, "skipped": skipped})
    db.commit()
    return {"upgraded": upgraded, "skipped": skipped, "proposal_id": str(proposal.id), "dry_run": False}


@router.post("/projects/{project_id}/registry/instantiate", status_code=201)
def instantiate(project_id: uuid.UUID, body: InstantiateIn, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create an application from a template. Same rules as creating one from a model (needs ``application.create`` and ``model.apply``)."""
    project = load_project(db, principal, project_id, "application.create")
    authorize(db, principal, project.organization_id, "model.apply", project_id=project.id)
    entry, slug = _find(db, principal, body.package, body.version, kind="template")
    v = _verified(request, entry)
    if db.scalar(select(Application.id).where(Application.project_id == project.id, Application.key == body.key)):
        raise api_error(409, "key_taken", "application key already in use in this project")
    app = Application(project_id=project.id, key=body.key, name=body.name)
    db.add(app)
    db.flush()
    try:
        RevisionRepository(db).create(app.id, assign_element_ids(template_model(v.model, key=body.key, name=body.name)), created_by=principal.user_id,
                                      message=f"From template {slug}/{entry.name}@{entry.version}", organization_id=project.organization_id)
    except ModelInvalid as e:
        db.rollback()
        raise api_error(422, "model_invalid", "the template does not pass this organization's checks", [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    audit(db, request, "registry.instantiated", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"package": f"{slug}/{entry.name}", "version": entry.version})
    db.commit()
    return {"application_id": str(app.id), "key": app.key, "package": f"{slug}/{entry.name}", "version": entry.version}


# --- other servers' registries -------------------------------------------------------------------------------------------


def _remote(db: Session, principal: Principal, org_id: uuid.UUID, name: str, action: str) -> RegistryRemote:
    authorize(db, principal, org_id, action)
    r = db.scalar(select(RegistryRemote).where(RegistryRemote.organization_id == org_id, RegistryRemote.name == name))
    if r is None:
        raise not_found("remote registry")
    return r


def remote_out(r: RegistryRemote) -> dict[str, Any]:
    return {"name": r.name, "base_url": r.base_url, "token_ref": r.token_ref, "remote_organization_id": r.remote_organization_id}


def _check_remote(request: Request, url: str, token_ref: str | None) -> None:
    if "?" in url or "#" in url or url.rstrip("/").endswith("/api/v1"):
        raise api_error(422, "invalid_remote", "give the server's address only (https://host[:port]), without a path, query or /api/v1")
    try:
        check_url(url, destination_policy(request.app.state.settings))
    except DestinationError as e:
        raise api_error(422, "destination_refused", str(e)) from e
    if token_ref is not None:
        if not SECRET_REF.match(token_ref):
            raise api_error(422, "invalid_secret_ref", "use env:NAME or file:/path")
        try:
            resolve_secret(token_ref)
        except SecretUnavailable as e:
            raise api_error(422, "secret_unavailable", str(e)) from e


MAX_REMOTE_BYTES = 17 * 1024 * 1024


def _call(request: Request, remote: RegistryRemote, method: str, path: str, *, body: bytes | None = None, content_type: str | None = None,
          max_bytes: int = 2 * 1024 * 1024) -> tuple[int, bytes]:
    """One request to the remote server's API over the SSRF-pinned transport. Its answers are data: errors never carry its text back to the caller."""
    headers = {"Accept": "application/json", "User-Agent": "Orchivio-Registry/1"}
    if remote.token_ref:
        try:
            headers["Authorization"] = f"Bearer {resolve_secret(remote.token_ref)}"
        except SecretUnavailable as e:
            raise api_error(409, "token_unavailable", "the access token for this remote registry is not available on this server") from e
    if content_type:
        headers["Content-Type"] = content_type
    try:
        resp = request.app.state.mcp_transport.request(method, f"{remote.base_url.rstrip('/')}/api/v1{path}", headers, body, timeout=20, max_bytes=max_bytes)
    except DestinationError as e:
        raise api_error(422, "destination_refused", str(e)) from e
    except OSError as e:
        raise api_error(502, "remote_unreachable", f"the remote registry could not be reached ({type(e).__name__})") from e
    return resp.status, resp.body


def _remote_json(status: int, data: bytes) -> Any:
    if status in (401, 403):
        raise api_error(502, "remote_refused", "the remote registry refused our credentials")
    if status == 404:
        raise api_error(404, "remote_not_found", "the remote registry has no such package")
    if status != 200:
        raise api_error(502, "remote_error", f"the remote registry answered HTTP {status}")
    try:
        return json.loads(data)
    except ValueError as e:
        raise api_error(502, "remote_error", "the remote registry sent something that is not JSON") from e


@router.put("/organizations/{org_id}/registry/remotes/{name}")
def put_remote(org_id: uuid.UUID, name: str, body: RemoteIn, request: Request, principal: Principal = Depends(current_principal),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    authorize(db, principal, org_id, "registry.publish")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise api_error(422, "invalid_name", "names are lower-case letters, digits and -")
    _check_remote(request, body.base_url, body.token_ref)
    r = db.scalar(select(RegistryRemote).where(RegistryRemote.organization_id == org_id, RegistryRemote.name == name))
    if r is None:
        r = RegistryRemote(organization_id=org_id, name=name, base_url=body.base_url, created_by=principal.user_id)
        db.add(r)
    r.base_url, r.token_ref, r.remote_organization_id = body.base_url.rstrip("/"), body.token_ref, body.remote_organization_id
    audit(db, request, "registry.remote_set", actor_id=principal.user_id, org_id=org_id, target_type="registry_remote", target_id=r.id,
          meta={"name": name, "authenticated": body.token_ref is not None})  # the URL may carry nothing secret, but it is not logged either
    db.commit()
    return remote_out(r)


@router.get("/organizations/{org_id}/registry/remotes")
def list_remotes(org_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    authorize(db, principal, org_id, "registry.publish")
    return {"items": [remote_out(r) for r in db.scalars(select(RegistryRemote).where(RegistryRemote.organization_id == org_id).order_by(RegistryRemote.name))]}


@router.delete("/organizations/{org_id}/registry/remotes/{name}", status_code=204)
def delete_remote(org_id: uuid.UUID, name: str, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    r = _remote(db, principal, org_id, name, "registry.publish")
    audit(db, request, "registry.remote_removed", actor_id=principal.user_id, org_id=org_id, target_type="registry_remote", target_id=r.id, meta={"name": name})
    db.delete(r)
    db.commit()
    return Response(status_code=204)


@router.get("/organizations/{org_id}/registry/remotes/{name}/packages")
def remote_search(org_id: uuid.UUID, name: str, request: Request, q: str | None = Query(None, max_length=100), kind: str | None = Query(None, pattern="^(module|template)$"),
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Search the remote registry (what that server shows to our token)."""
    r = _remote(db, principal, org_id, name, "registry.publish")
    qs = "&".join(p for p in (f"q={q}" if q else "", f"kind={kind}" if kind else "", "limit=50") if p)
    status, data = _call(request, r, "GET", f"/registry/packages?{qs}")
    doc = _remote_json(status, data)
    items = doc.get("items", []) if isinstance(doc, dict) else []
    keep = ("package", "name", "organization", "version", "kind", "description", "license", "versions", "key_id", "contents")
    return {"items": [{k: i.get(k) for k in keep} for i in items if isinstance(i, dict)]}


@router.post("/organizations/{org_id}/registry/remotes/{name}/pull", status_code=201)
def remote_pull(org_id: uuid.UUID, name: str, body: RemotePullIn, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    """Fetch a package from the remote registry into this organization's. It is stored only if it is signed by a key this server trusts."""
    r = _remote(db, principal, org_id, name, "registry.publish")
    m = REF.match(body.package)
    if not m:
        raise api_error(422, "invalid_package", "use <organization-slug>/<name>")
    status, data = _call(request, r, "GET", f"/registry/packages/{m.group(1)}/{m.group(2)}")
    versions = [v["version"] for v in _remote_json(status, data).get("versions", []) if isinstance(v, dict) and not v.get("yanked") and isinstance(v.get("version"), str)]
    spec = body.version.strip()
    try:
        picked = str(Version.parse(spec)) if spec[:1].isdigit() else resolve(spec, versions)
    except VersionError as e:
        raise api_error(422, "invalid_version", str(e)) from e
    if picked not in versions:
        raise api_error(404, "no_matching_version", f"the remote registry has no available version of '{body.package}' matching '{body.version}'", {"available": sorted(versions)})
    status, blob = _call(request, r, "GET", f"/registry/packages/{m.group(1)}/{m.group(2)}/{picked}/download", max_bytes=MAX_REMOTE_BYTES)
    if status != 200:
        _remote_json(status, b"")
    return _store(db, request, principal, org_id, blob, body.visibility, action="registry.pulled", extra_meta={"remote": name, "from": body.package})


@router.post("/organizations/{org_id}/registry/remotes/{name}/push")
def remote_push(org_id: uuid.UUID, name: str, body: RemotePushIn, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    """Send one of this organization's packages to the remote registry. The remote accepts it only if it trusts the signing key."""
    r = _remote(db, principal, org_id, name, "registry.publish")
    if not r.remote_organization_id:
        raise api_error(422, "remote_organization_required", "set remote_organization_id on this remote to say where pushes go")
    m = REF.match(body.package)
    org = db.get(Organization, org_id)
    if not m or m.group(1) != org.slug:  # type: ignore[union-attr]
        raise api_error(422, "invalid_package", "you can push only this organization's own packages (<its slug>/<name>)")
    entry = db.scalar(select(RegistryEntry).where(RegistryEntry.organization_id == org_id, RegistryEntry.name == m.group(2), RegistryEntry.version == body.version))
    if entry is None:
        raise not_found("package version")
    blob = request.app.state.artifacts.get(entry.artifact_key)
    status, data = _call(request, r, "POST", f"/organizations/{r.remote_organization_id}/registry/import?visibility=org", body=blob, content_type="application/zip", max_bytes=2 * 1024 * 1024)
    if status == 201:
        audit(db, request, "registry.pushed", actor_id=principal.user_id, org_id=org_id, target_type="registry_entry", target_id=entry.id,
              meta={"remote": name, "name": entry.name, "version": entry.version})
        db.commit()
        return {"pushed": True, "package": body.package, "version": entry.version, "remote": name}
    if status == 409:
        raise api_error(409, "remote_has_it", "the remote registry already has this version, or a package of that name with another kind")
    if status == 422:
        raise api_error(422, "remote_rejected", "the remote registry rejected the package (most likely it does not trust the signing key)")
    _remote_json(status, data)
    raise api_error(502, "remote_error", f"the remote registry answered HTTP {status}")
