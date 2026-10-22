"""Environments, constants, signed deployment packages and gated promotion (docs/DELIVERY.md, FR-094..096).

There is no deployment target yet: a deployment is the platform's record of which signed package is live in
which stage, with every gate and approval behind it. Running that package elsewhere is outside this slice.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchivio import __version__
from orchivio.db import to_model
from orchivio.db.models import (
    AppRevision,
    Build,
    DeploymentPackage,
    Environment,
    EnvironmentConstant,
    PackageDeployment,
    TestRun,
)
from orchivio.delivery import PackageError, build_package, public_key_of, run_contract, run_expand, verify_package
from orchivio.delivery.package import VerifiedPackage
from orchivio.runtime import table_prefix
from orchivio.runtime.production import ProductionStore

from ..deps import (
    Principal,
    api_error,
    audit,
    current_principal,
    get_db,
    load_application,
    load_revision,
    not_found,
    utcnow,
)
from ..schemas import Page

router = APIRouter(tags=["delivery"])

ENV_NAME = r"^[a-z][a-z0-9-]{0,39}$"
SECRET_REF = re.compile(r"^(env|vault|file|aws-sm|gcp-sm|azure-kv):[A-Za-z0-9_./:@-]{1,250}$")
SECRETISH = re.compile(r"(secret|password|passwd|token|api[_-]?key|private|credential)", re.I)


class EnvIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=ENV_NAME)
    stage_order: int = Field(ge=0, le=100)
    requires_approval: bool = False
    require_tests: bool = True


class EnvPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requires_approval: bool | None = None
    require_tests: bool | None = None


class ConstantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None = Field(default=None, max_length=2000)
    secret_ref: str | None = Field(default=None, max_length=300)


class PackageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous_package_id: uuid.UUID | None = Field(
        default=None, description="The package currently deployed to the stage this one is meant for; its model is the migration base")


class PromoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str = Field(pattern=ENV_NAME)
    acknowledge_risks: bool = False
    comment: str | None = Field(default=None, max_length=1000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str | None = Field(default=None, max_length=1000)


class MigrationRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str = Field(pattern="^(expand|contract)$", default="expand")
    dry_run: bool = False
    acknowledge_risks: bool = False  # required when phase=contract


def _iso(d):
    return d.isoformat() if d else None


def env_out(e: Environment) -> dict[str, Any]:
    return {"id": str(e.id), "application_id": str(e.application_id), "name": e.name, "stage_order": e.stage_order,
            "requires_approval": e.requires_approval, "require_tests": e.require_tests}


def package_out(p: DeploymentPackage) -> dict[str, Any]:
    return {"id": str(p.id), "application_id": str(p.application_id), "revision_id": str(p.revision_id),
            "runtime_version": p.runtime_version, "model_hash": p.model_hash, "digest": p.digest, "key_id": p.key_id,
            "previous_model_hash": p.manifest.get("previous_model_hash"), "created_at": _iso(p.created_at)}


def deployment_out(d: PackageDeployment) -> dict[str, Any]:
    return {"id": str(d.id), "environment_id": str(d.environment_id), "package_id": str(d.package_id), "status": d.status,
            "requested_by": str(d.requested_by) if d.requested_by else None, "decided_by": str(d.decided_by) if d.decided_by else None,
            "comment": d.comment, "risks_acknowledged": d.risks_acknowledged, "created_at": _iso(d.created_at),
            "decided_at": _iso(d.decided_at)}


# --- keys and artifacts ------------------------------------------------------------------------


def _seed(request: Request) -> str:
    key = request.app.state.settings.package_signing_key
    if key is None:
        raise api_error(503, "signing_not_configured", "set ORCHIVIO_PACKAGE_SIGNING_KEY (see `orchivio keygen`) to build packages")
    return key.get_secret_value()


def _trusted(request: Request) -> list[str]:
    s = request.app.state.settings
    keys = list(s.package_trusted_public_keys)
    if s.package_signing_key is not None:
        keys.append(public_key_of(s.package_signing_key.get_secret_value()))
    return keys


def _verified(request: Request, pkg: DeploymentPackage) -> VerifiedPackage:
    """Re-read the stored bytes and check them against the database record and the signature."""
    store = request.app.state.artifacts
    try:
        data = store.get(pkg.artifact_key)
    except (FileNotFoundError, KeyError, ValueError) as e:
        raise api_error(409, "package_corrupt", "the package file is missing from the artifact store") from e
    try:
        v = verify_package(data, _trusted(request))
    except PackageError as e:
        raise api_error(409, "package_corrupt", f"the package failed verification: {e}") from e
    if v.digest != pkg.digest:
        raise api_error(409, "package_corrupt", "the package file does not match its recorded digest")
    return v


# --- environments ------------------------------------------------------------------------------


def _load_env(db: Session, principal: Principal, env_id: uuid.UUID, action: str) -> tuple[Environment, Any, Any]:
    env = db.get(Environment, env_id)
    if env is None:
        raise not_found("environment")
    app, project = load_application(db, principal, env.application_id, action)
    return env, app, project


def _envs(db: Session, app_id: uuid.UUID) -> list[Environment]:
    return list(db.scalars(select(Environment).where(Environment.application_id == app_id).order_by(Environment.stage_order)))


@router.get("/applications/{app_id}/environments")
def list_environments(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    out = []
    for e in _envs(db, app.id):
        cur = _current(db, e.id)
        out.append({**env_out(e), "current": deployment_out(cur) if cur else None,
                    "current_package": package_out(db.get(DeploymentPackage, cur.package_id)) if cur else None})
    return {"items": out}


@router.post("/applications/{app_id}/environments", status_code=201)
def create_environment(app_id: uuid.UUID, body: EnvIn, request: Request, principal: Principal = Depends(current_principal),
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    app, project = load_application(db, principal, app_id, "delivery.manage")
    env = Environment(application_id=app.id, name=body.name, stage_order=body.stage_order,
                      requires_approval=body.requires_approval, require_tests=body.require_tests)
    db.add(env)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise api_error(409, "environment_exists", "an environment with this name or stage order already exists") from e
    audit(db, request, "environment.created", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="environment", target_id=env.id, meta={"name": env.name, "stage_order": env.stage_order})
    db.commit()
    return env_out(env)


@router.patch("/environments/{env_id}")
def update_environment(env_id: uuid.UUID, body: EnvPatch, request: Request, principal: Principal = Depends(current_principal),
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    env, _, project = _load_env(db, principal, env_id, "delivery.manage")
    changed = {k: v for k, v in body.model_dump().items() if v is not None}
    for k, v in changed.items():
        setattr(env, k, v)
    audit(db, request, "environment.updated", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="environment", target_id=env.id, meta={"name": env.name, **changed})
    db.commit()
    return env_out(env)


@router.delete("/environments/{env_id}", status_code=204)
def delete_environment(env_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                       db: Session = Depends(get_db)) -> Response:
    env, _, project = _load_env(db, principal, env_id, "delivery.manage")
    if db.scalar(select(func.count()).select_from(PackageDeployment).where(PackageDeployment.environment_id == env.id)):
        raise api_error(409, "environment_in_use", "an environment with deployment history cannot be deleted")
    audit(db, request, "environment.deleted", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="environment", target_id=env.id, meta={"name": env.name})
    db.delete(env)
    db.commit()
    return Response(status_code=204)


# --- constants and secret references -----------------------------------------------------------


@router.get("/environments/{env_id}/constants")
def list_constants(env_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    env, _, _ = _load_env(db, principal, env_id, "model.read")
    rows = db.scalars(select(EnvironmentConstant).where(EnvironmentConstant.environment_id == env.id).order_by(EnvironmentConstant.key))
    return {"items": [{"key": c.key, "value": c.value, "secret_ref": c.secret_ref} for c in rows]}


@router.put("/environments/{env_id}/constants/{key}")
def put_constant(env_id: uuid.UUID, key: str, body: ConstantIn, request: Request, principal: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    """A constant is a plain value or a reference to a secret store. Secrets themselves are never stored."""
    env, _, project = _load_env(db, principal, env_id, "delivery.manage")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key):
        raise api_error(422, "invalid_key", "keys are letters, digits and underscores, starting with a letter (max 64)")
    if (body.value is None) == (body.secret_ref is None):
        raise api_error(422, "invalid_constant", "give exactly one of value and secret_ref")
    if body.secret_ref is not None and not SECRET_REF.match(body.secret_ref):
        raise api_error(422, "invalid_secret_ref", "use scheme:path with scheme env, vault, file, aws-sm, gcp-sm or azure-kv")
    if body.value is not None and SECRETISH.search(key):
        raise api_error(422, "secret_in_value", "this name looks like a secret; store a secret_ref instead of the value")
    row = db.scalar(select(EnvironmentConstant).where(EnvironmentConstant.environment_id == env.id, EnvironmentConstant.key == key))
    if row is None:
        row = EnvironmentConstant(environment_id=env.id, key=key)
        db.add(row)
    row.value, row.secret_ref = body.value, body.secret_ref
    audit(db, request, "environment.constant_set", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="environment", target_id=env.id, meta={"key": key, "kind": "secret_ref" if body.secret_ref else "value"})
    db.commit()
    return {"key": key, "value": row.value, "secret_ref": row.secret_ref}


@router.delete("/environments/{env_id}/constants/{key}", status_code=204)
def delete_constant(env_id: uuid.UUID, key: str, request: Request, principal: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)) -> Response:
    env, _, project = _load_env(db, principal, env_id, "delivery.manage")
    row = db.scalar(select(EnvironmentConstant).where(EnvironmentConstant.environment_id == env.id, EnvironmentConstant.key == key))
    if row is None:
        raise not_found("constant")
    db.delete(row)
    audit(db, request, "environment.constant_deleted", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="environment", target_id=env.id, meta={"key": key})
    db.commit()
    return Response(status_code=204)


# --- packages ----------------------------------------------------------------------------------


@router.post("/revisions/{revision_id}/packages")
def build(revision_id: uuid.UUID, body: PackageIn, request: Request, response: Response,
          principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Build the signed, reproducible package of a revision. The same inputs always return the same package."""
    rev, app, project = load_revision(db, principal, revision_id, "package.build")
    seed = _seed(request)
    previous = None
    if body.previous_package_id is not None:
        prev = db.get(DeploymentPackage, body.previous_package_id)
        if prev is None or prev.application_id != app.id:
            raise not_found("package")
        previous = to_model(db.get(AppRevision, prev.revision_id))
    try:
        pkg = build_package(to_model(rev), runtime_version=__version__, signing_seed=seed, previous=previous)
    except PackageError as e:
        raise api_error(503, "signing_not_configured", str(e)) from e
    existing = db.scalar(select(DeploymentPackage).where(DeploymentPackage.digest == pkg.digest))
    if existing is not None:
        return package_out(existing)  # identical inputs: same package, nothing new stored
    request.app.state.artifacts.put(f"packages/{pkg.digest}.zip", pkg.data)
    row = DeploymentPackage(application_id=app.id, revision_id=rev.id, runtime_version=__version__, model_hash=rev.model_hash,
                            digest=pkg.digest, key_id=pkg.key_id, signature=pkg.signature, artifact_key=f"packages/{pkg.digest}.zip",
                            manifest=pkg.manifest, created_by=principal.user_id)
    db.add(row)
    db.flush()
    audit(db, request, "package.built", actor_id=principal.user_id, org_id=project.organization_id, target_type="package",
          target_id=row.id, meta={"revision_no": rev.revision_no, "digest": pkg.digest, "key_id": pkg.key_id})
    db.commit()
    response.status_code = 201
    return package_out(row)


def _load_package(db: Session, principal: Principal, package_id: uuid.UUID, action: str):
    pkg = db.get(DeploymentPackage, package_id)
    if pkg is None:
        raise not_found("package")
    app, project = load_application(db, principal, pkg.application_id, action)
    return pkg, app, project


@router.get("/applications/{app_id}/packages", response_model=Page[dict])
def list_packages(app_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    q = select(DeploymentPackage).where(DeploymentPackage.application_id == app.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(desc(DeploymentPackage.created_at), DeploymentPackage.id).limit(limit).offset(offset)).all()
    return Page(items=[package_out(p) for p in rows], limit=limit, offset=offset, total=total)


@router.get("/packages/{package_id}")
def get_package(package_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    """Metadata, plus the migration plan and SBOM read from the verified package."""
    pkg, _, _ = _load_package(db, principal, package_id, "model.read")
    v = _verified(request, pkg)
    return {**package_out(pkg), "migration_plan": v.migration_plan, "sbom": v.sbom}


@router.get("/packages/{package_id}/verify")
def verify(package_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    pkg, _, _ = _load_package(db, principal, package_id, "model.read")
    v = _verified(request, pkg)
    return {"valid": True, "digest": v.digest, "key_id": v.manifest["key_id"], "model_hash": v.manifest["model_hash"]}


@router.get("/packages/{package_id}/download")
def download(package_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
             db: Session = Depends(get_db)) -> Response:
    """The package file (admin: it contains the whole model)."""
    pkg, _, project = _load_package(db, principal, package_id, "project.export")
    _verified(request, pkg)
    audit(db, request, "package.downloaded", actor_id=principal.user_id, org_id=project.organization_id, target_type="package",
          target_id=pkg.id, meta={"digest": pkg.digest})
    db.commit()
    return Response(request.app.state.artifacts.get(pkg.artifact_key), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="package-{pkg.digest[:12]}.zip"'})


# --- promotion ---------------------------------------------------------------------------------


def _current(db: Session, env_id: uuid.UUID) -> PackageDeployment | None:
    return db.scalar(select(PackageDeployment).where(PackageDeployment.environment_id == env_id, PackageDeployment.status == "deployed")
                     .order_by(desc(PackageDeployment.decided_at), desc(PackageDeployment.created_at)).limit(1))


def _destructive(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in plan.get("steps", []) if s.get("destructive")]


def _migration_base(db: Session, env: Environment) -> tuple[str | None, uuid.UUID | None]:
    cur = _current(db, env.id)
    if cur is None:
        return None, None
    p = db.get(DeploymentPackage, cur.package_id)
    return p.model_hash, p.id


def _check_base(db: Session, env: Environment, v: VerifiedPackage) -> None:
    expected, expected_pkg = _migration_base(db, env)
    if v.manifest.get("previous_model_hash") != expected:
        raise api_error(409, "migration_base_mismatch",
                        "this package's migration plan starts from a different model than the one deployed in "
                        f"'{env.name}'; build it again with previous_package_id set to the deployed package",
                        {"deployed_package_id": str(expected_pkg) if expected_pkg else None})


def _go_live(db: Session, dep: PackageDeployment, now) -> None:
    for old in db.scalars(select(PackageDeployment).where(PackageDeployment.environment_id == dep.environment_id,
                                                         PackageDeployment.status == "deployed", PackageDeployment.id != dep.id)):
        old.status = "superseded"
    dep.status, dep.decided_at = "deployed", now


@router.post("/packages/{package_id}/promote", status_code=201)
def promote(package_id: uuid.UUID, body: PromoteIn, request: Request, principal: Principal = Depends(current_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    """Request that a package go live in an environment. Gates, in order: the package verifies, the stage before
    it already runs this exact package, tests passed (if the stage requires them), the migration starts from what
    is deployed, destructive steps are acknowledged, and the stage's approval policy."""
    pkg, app, project = _load_package(db, principal, package_id, "deployment.request")
    env = db.scalar(select(Environment).where(Environment.application_id == app.id, Environment.name == body.environment))
    if env is None:
        raise not_found("environment")
    v = _verified(request, pkg)

    envs = _envs(db, app.id)
    earlier = [e for e in envs if e.stage_order < env.stage_order]
    if earlier:
        prev_env = earlier[-1]
        cur = _current(db, prev_env.id)
        if cur is None or cur.package_id != pkg.id:
            raise api_error(409, "stage_skipped", f"deploy this package to '{prev_env.name}' first", {"required_environment": prev_env.name})
    now_cur = _current(db, env.id)
    if now_cur is not None and now_cur.package_id == pkg.id:
        raise api_error(409, "already_deployed", f"this package is already live in '{env.name}'")
    if db.scalar(select(PackageDeployment.id).where(PackageDeployment.environment_id == env.id, PackageDeployment.package_id == pkg.id,
                                                    PackageDeployment.status == "pending_approval")):
        raise api_error(409, "already_pending", "a deployment of this package to this environment awaits approval")
    if env.require_tests and not db.scalar(select(TestRun.id).join(Build, TestRun.build_id == Build.id)
                                           .where(Build.revision_id == pkg.revision_id, TestRun.status == "succeeded").limit(1)):
        raise api_error(409, "tests_required", f"'{env.name}' requires a passing test run of this revision first")
    _check_base(db, env, v)
    risky = _destructive(v.migration_plan)
    if risky and not body.acknowledge_risks:
        raise api_error(409, "approval_required", "the migration removes or narrows data; resend with acknowledge_risks=true",
                        {"destructive_steps": risky})

    now = utcnow()
    dep = PackageDeployment(environment_id=env.id, package_id=pkg.id, status="pending_approval", requested_by=principal.user_id,
                            comment=body.comment, risks_acknowledged=bool(risky), created_at=now)
    db.add(dep)
    db.flush()
    if not env.requires_approval:
        _go_live(db, dep, now)
    audit(db, request, "deployment.requested" if env.requires_approval else "deployment.deployed", actor_id=principal.user_id,
          org_id=project.organization_id, target_type="deployment", target_id=dep.id,
          meta={"environment": env.name, "package": str(pkg.id), "digest": pkg.digest, "destructive_steps": len(risky)})
    db.commit()
    return deployment_out(dep)


@router.post("/deployments/{deployment_id}/approval")
def decide(deployment_id: uuid.UUID, body: DecisionIn, request: Request, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    """Approve or reject a pending deployment. The approver must not be the requester."""
    dep = db.get(PackageDeployment, deployment_id)
    if dep is None:
        raise not_found("deployment")
    env, app, project = _load_env(db, principal, dep.environment_id, "deployment.approve")
    if dep.status != "pending_approval":
        raise api_error(409, "not_pending", "this deployment has already been decided")
    if dep.requested_by == principal.user_id:
        raise api_error(403, "separation_of_duties", "someone other than the requester must decide")
    now = utcnow()
    if body.decision == "approve":
        pkg = db.get(DeploymentPackage, dep.package_id)
        v = _verified(request, pkg)  # the bytes may have changed since the request
        _check_base(db, env, v)
        _go_live(db, dep, now)
        # FR-094: provision production tables when the environment has a data_url
        if env.data_url:
            from orchivio.db import to_model as _to_model
            rev = db.get(AppRevision, v.manifest.get("revision_id")) if v.manifest.get("revision_id") else None
            if rev is not None:
                try:
                    prod = ProductionStore(env.data_url)
                    prod.build(app.id, _to_model(rev), rev.model_hash or "")
                except Exception:  # noqa: BLE001 - provisioning errors must not block the approval response
                    import logging as _logging
                    _logging.getLogger("orchivio.delivery").exception("production store build failed for env %s", env.id)
    else:
        dep.status, dep.decided_at = "rejected", now
    dep.decided_by, dep.comment = principal.user_id, body.comment or dep.comment
    audit(db, request, f"deployment.{'deployed' if body.decision == 'approve' else 'rejected'}", actor_id=principal.user_id,
          org_id=project.organization_id, target_type="deployment", target_id=dep.id,
          meta={"environment": env.name, "package": str(dep.package_id), "requested_by": str(dep.requested_by)})
    db.commit()
    return deployment_out(dep)


@router.get("/applications/{app_id}/deployments", response_model=Page[dict])
def list_deployments(app_id: uuid.UUID, environment: str | None = Query(None, pattern=ENV_NAME), status: str | None = Query(
        None, pattern="^(pending_approval|deployed|rejected|superseded)$"), limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0), principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[Any]:
    app, _ = load_application(db, principal, app_id, "model.read")
    q = select(PackageDeployment).join(Environment, Environment.id == PackageDeployment.environment_id).where(Environment.application_id == app.id)
    if environment:
        q = q.where(Environment.name == environment)
    if status:
        q = q.where(PackageDeployment.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(desc(PackageDeployment.created_at), PackageDeployment.id).limit(limit).offset(offset)).all()
    return Page(items=[deployment_out(d) for d in rows], limit=limit, offset=offset, total=total)


# --- FR-097: migration executor ---------------------------------------------------------------


@router.post("/packages/{package_id}/migrations/run")
def run_migration(package_id: uuid.UUID, body: MigrationRunIn, request: Request,
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Apply the expand or contract phase of a package's migration plan against the preview database (FR-097).

    Expand phase is safe to run before upgrading the running code. Contract phase is destructive
    (drops columns / tables) and requires ``acknowledge_risks=true``.

    Pass ``dry_run=true`` to preview the SQL operations without applying them.
    """
    pkg, app, project = _load_package(db, principal, package_id, "delivery.manage")
    v = _verified(request, pkg)

    steps: list[dict[str, Any]] = v.migration_plan.get("steps", [])
    if not steps:
        return {"applied": [], "dry_run": body.dry_run, "phase": body.phase}

    if body.phase == "contract":
        destructive = [s for s in steps if s.get("phase") == "contract"]
        if destructive and not body.acknowledge_risks:
            raise api_error(409, "approval_required",
                            "contract migrations drop or narrow data; resend with acknowledge_risks=true",
                            {"destructive_steps": destructive})

    prefix = table_prefix(app.id)
    # FR-097: migrate against production data_url when the deployment targets an env with one, else preview
    target_engine = request.app.state.preview_store.engine
    if hasattr(pkg, "environment_id") and pkg.environment_id:
        env = db.get(Environment, pkg.environment_id)
        if env is not None and env.data_url:
            target_engine = ProductionStore(env.data_url).engine
    applied: list[str] = []

    if body.dry_run:
        # Return what would be applied without touching the database
        dummy_steps = [s for s in steps if s.get("phase") == body.phase]
        applied = [f"{s['op']} {s['target']}" for s in dummy_steps]
    else:
        with target_engine.begin() as conn:
            if body.phase == "expand":
                applied = run_expand(conn, steps, prefix)
            else:
                applied = run_contract(conn, steps, prefix)

    audit(db, request, f"migration.{body.phase}_{'dry_run' if body.dry_run else 'applied'}",
          actor_id=principal.user_id, org_id=project.organization_id, target_type="package", target_id=pkg.id,
          meta={"ops_count": len(applied), "dry_run": body.dry_run})
    db.commit()
    return {"applied": applied, "dry_run": body.dry_run, "phase": body.phase, "ops_count": len(applied)}
