"""Module and template registry (FR-126, docs/REGISTRY.md).

Publishing, importing and withdrawing need ``registry.publish`` (organization admins). Browsing is open to members of the publishing
organization, and to everyone signed in for packages published with ``visibility: instance``. Installing a module only ever creates
a proposal; creating an application from a template goes through the same checks as creating one from a model.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio import __version__
from orchivio.db import ModelInvalid, ProposalRepository, RevisionRepository, to_model
from orchivio.db.models import (
    Application,
    AppRevision,
    Membership,
    Organization,
    RegistryEntry,
    RegistryInstall,
)
from orchivio.db.proposals import NoChanges
from orchivio.delivery.package import public_key_of
from orchivio.model import assign_element_ids
from orchivio.registry import (
    RegistryError,
    Version,
    VersionError,
    build_package,
    merge_module,
    module_hash,
    resolve,
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


class YankIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=300)


class InstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(max_length=130, description="<organization-slug>/<name>")
    version: str = Field(default="latest", max_length=64, description="1.2.3, ^1.2.0, ~1.2.0, >=1.2.0 or latest")
    overwrite_local_changes: bool = False


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
    try:
        if body.kind == "module":
            if not body.module:
                raise api_error(422, "module_required", "give the key of the module to publish")
            model = slice_module(model, body.module, name=body.name)
        pkg = build_package(model, name=body.name, version=body.version, kind=body.kind, description=body.description, license=body.license,
                            runtime_version=".".join(map(str, _core(__version__))), signing_seed=key.get_secret_value())
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
    slug = db.get(Organization, org_id).slug  # type: ignore[union-attr]
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
        audit(db, request, "registry.imported", actor_id=principal.user_id, org_id=org_id, target_type="registry_entry", target_id=entry.id,
              meta={"name": entry.name, "version": entry.version, "key_id": entry.key_id, "digest": v.digest})
        db.commit()
        return entry_out(entry, db.get(Organization, org_id).slug)  # type: ignore[union-attr]

    return await run_in_threadpool(work)


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


@router.post("/applications/{app_id}/registry/install", status_code=201)
def install(app_id: uuid.UUID, body: InstallIn, request: Request, principal: Principal = Depends(current_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    """Add (or upgrade) a module as a *proposal*. Roles and access rules the module needs appear in the diff like any other change."""
    app, project = load_application(db, principal, app_id, "model.propose")
    entry, slug = _find(db, principal, body.package, body.version, kind="module")
    v = _verified(request, entry)
    mod = v.model.modules[0]
    target = _current(db, app)
    ref = f"{slug}/{entry.name}"
    history = list(db.scalars(select(RegistryInstall).where(RegistryInstall.application_id == app.id, RegistryInstall.module_key == mod.key)
                              .order_by(RegistryInstall.created_at.desc())))
    exists = any(m.key == mod.key for m in target.modules)
    replace = False
    if exists:
        if not any(r.package_ref == ref for r in history):
            raise api_error(409, "module_exists", f"the application already has a module '{mod.key}' that did not come from '{ref}'")
        current = module_hash(target, mod.key)
        if not any(r.package_ref == ref and r.module_hash == current for r in history) and not body.overwrite_local_changes:
            raise api_error(409, "module_modified", f"module '{mod.key}' was changed after it was installed; pass overwrite_local_changes to replace those changes")
        replace = True
    try:
        merged = merge_module(target, v.model, replace=replace)
    except RegistryError as e:
        raise _fail(e, 409) from e
    added_roles = [r for r in v.model.security.roles if r not in target.security.roles]
    try:
        proposal = ProposalRepository(db).create(app.id, assign_element_ids(merged, previous=target), source="human",
                                                 title=f"{'Upgrade' if replace else 'Install'} module {ref}@{entry.version}",
                                                 created_by=principal.user_id, organization_id=project.organization_id)
    except ModelInvalid as e:
        raise api_error(422, "model_invalid", "the module does not fit this application", [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    except NoChanges as e:
        raise api_error(409, "already_installed", "this version is already part of the application") from e
    db.add(RegistryInstall(application_id=app.id, package_ref=ref, version=entry.version, module_key=mod.key,
                           module_hash=module_hash(v.model, mod.key) or "", proposal_id=proposal.id, installed_by=principal.user_id))
    audit(db, request, "registry.install_proposed", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"package": ref, "version": entry.version, "module": mod.key, "upgrade": replace, "added_roles": added_roles, "proposal_id": str(proposal.id)})
    db.commit()
    return {"proposal": proposal_detail(proposal).model_dump(mode="json"), "package": ref, "version": entry.version, "module": mod.key, "upgrade": replace, "added_roles": added_roles}


@router.get("/applications/{app_id}/registry/installs")
def installs(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Modules installed from the registry, whether they are applied or modified, and whether a newer version exists."""
    app, _ = load_application(db, principal, app_id, "model.read")
    rows = list(db.scalars(select(RegistryInstall).where(RegistryInstall.application_id == app.id).order_by(RegistryInstall.created_at.desc())))
    model = _current(db, app) if app.current_revision_id else None
    by_module: dict[str, list[RegistryInstall]] = {}
    for r in rows:
        by_module.setdefault(r.module_key, []).append(r)
    out = []
    for key, history in by_module.items():
        current = module_hash(model, key) if model else None
        applied = next((r for r in history if current is not None and r.module_hash == current), None)
        r = applied or history[0]
        state = "not_applied" if current is None else "installed" if applied else "modified"
        try:
            newest = _find(db, principal, r.package_ref, "latest")[0].version
        except Exception:  # noqa: BLE001 - the package may be gone or no longer visible; that is not an error here
            newest = None
        out.append({"module": key, "package": r.package_ref, "version": r.version, "state": state, "proposal_id": str(r.proposal_id) if r.proposal_id else None,
                    "latest_version": newest, "update_available": bool(newest and Version.parse(newest) > Version.parse(r.version))})
    return {"items": out}


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

