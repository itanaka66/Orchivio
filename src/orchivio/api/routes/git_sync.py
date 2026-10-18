"""Git synchronisation of an application (docs/GIT-SYNC.md).

Configuration needs ``gitsync.manage`` (admin, A3). Pushing exports the model to an external system, so it needs ``project.export``
(admin). Pulling creates a *proposal* like any other change and needs ``model.propose``. The token is only ever a reference
(``env:NAME`` or ``file:/path``) resolved at call time.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchivio import telemetry
from orchivio.db import ModelInvalid, ProposalRepository, to_model
from orchivio.db.models import AppRevision, GitSync
from orchivio.db.proposals import NoChanges
from orchivio.gitsync import GitSyncError, valid_branch, valid_subdir
from orchivio.gitsync.github import GitHubPullRequests
from orchivio.integrations import SecretUnavailable, resolve_secret
from orchivio.integrations.dispatcher import SECRET_REF
from orchivio.model import CanonicalError, from_files, to_files

from ..deps import (
    Principal,
    api_error,
    audit,
    current_principal,
    get_db,
    load_application,
    not_found,
    utcnow,
)
from .proposals import proposal_detail

router = APIRouter(tags=["git-sync"])

_locks: dict[uuid.UUID, threading.Lock] = {}
_locks_guard = threading.Lock()

_STATUS = {"git_sync_disabled": 409, "invalid_remote": 422, "host_not_allowed": 422, "invalid_config": 422, "invalid_files": 422, "unsafe_entry": 422,
           "too_large": 422, "repo_too_large": 422, "branch_not_found": 404, "path_not_found": 404}


def _fail(e: GitSyncError):
    return api_error(_STATUS.get(e.code, 502), e.code, e.message)


class SyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_url: str = Field(min_length=1, max_length=500)
    base_branch: str = Field(default="main", max_length=100)
    push_branch: str | None = Field(default=None, max_length=100)
    path: str = Field(default="", max_length=100)
    token_ref: str | None = Field(default=None, max_length=300)
    provider: str = Field(default="git", pattern="^(git|github)$")


class PushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: uuid.UUID | None = None
    open_pull_request: bool = False
    message: str | None = Field(default=None, max_length=200)


def sync_out(g: GitSync) -> dict[str, Any]:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {"remote_url": g.remote_url, "base_branch": g.base_branch, "push_branch": g.push_branch, "path": g.path, "token_ref": g.token_ref,
            "provider": g.provider, "last_push_at": iso(g.last_push_at), "last_push_commit": g.last_push_commit,
            "last_push_revision_id": str(g.last_push_revision_id) if g.last_push_revision_id else None,
            "last_pull_at": iso(g.last_pull_at), "last_pull_commit": g.last_pull_commit}


def _config(db: Session, app_id: uuid.UUID) -> GitSync:
    g = db.scalar(select(GitSync).where(GitSync.application_id == app_id))
    if g is None:
        raise not_found("git sync configuration")
    return g


def _token(g: GitSync) -> str | None:
    if not g.token_ref:
        return None
    try:
        return resolve_secret(g.token_ref)
    except SecretUnavailable as e:
        raise api_error(409, "token_unavailable", "the access token for this repository is not available on this server") from e


class _Busy:
    def __init__(self, app_id: uuid.UUID) -> None:
        with _locks_guard:
            self.lock = _locks.setdefault(app_id, threading.Lock())

    def __enter__(self) -> None:
        if not self.lock.acquire(blocking=False):
            raise api_error(409, "sync_in_progress", "another Git sync of this application is running")

    def __exit__(self, *exc: object) -> None:
        self.lock.release()


@router.put("/applications/{app_id}/git-sync")
def configure(app_id: uuid.UUID, body: SyncIn, request: Request, principal: Principal = Depends(current_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    app, project = load_application(db, principal, app_id, "gitsync.manage")
    svc = request.app.state.git_sync
    push_branch = body.push_branch or f"orchivio/{app.key}"
    try:
        svc.validate_remote(body.remote_url, resolve=False)
    except GitSyncError as e:
        raise _fail(e) from e
    if not (valid_branch(body.base_branch) and valid_branch(push_branch) and valid_subdir(body.path)):
        raise api_error(422, "invalid_config", "invalid branch name or path")
    if push_branch == body.base_branch:
        raise api_error(422, "invalid_config", "the push branch must differ from the base branch; changes reach it through a pull request")
    if body.token_ref is not None and not SECRET_REF.match(body.token_ref):
        raise api_error(422, "invalid_secret_ref", "use env:NAME or file:/path")
    g = db.scalar(select(GitSync).where(GitSync.application_id == app.id))
    if g is None:
        g = GitSync(application_id=app.id, created_by=principal.user_id, remote_url=body.remote_url, push_branch=push_branch)
        db.add(g)
    g.remote_url, g.base_branch, g.push_branch, g.path, g.token_ref, g.provider = (
        body.remote_url, body.base_branch, push_branch, body.path, body.token_ref, body.provider)
    audit(db, request, "gitsync.configured", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"base_branch": g.base_branch, "push_branch": g.push_branch, "path": g.path, "provider": g.provider, "has_token": g.token_ref is not None})
    db.commit()
    return sync_out(g)


@router.get("/applications/{app_id}/git-sync")
def get_config(app_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    load_application(db, principal, app_id, "gitsync.manage")
    return sync_out(_config(db, app_id))


@router.delete("/applications/{app_id}/git-sync", status_code=204)
def remove(app_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    _, project = load_application(db, principal, app_id, "gitsync.manage")
    db.delete(_config(db, app_id))
    audit(db, request, "gitsync.removed", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app_id)
    db.commit()
    return Response(status_code=204)


@router.post("/applications/{app_id}/git-sync/push")
def push(app_id: uuid.UUID, body: PushIn, request: Request, principal: Principal = Depends(current_principal),
         db: Session = Depends(get_db)) -> dict[str, Any]:
    """Commit a revision's canonical files to the push branch (force-updated: it belongs to Orchivio) and optionally open a pull request."""
    app, project = load_application(db, principal, app_id, "project.export")
    g = _config(db, app.id)
    rev_id = body.revision_id or app.current_revision_id
    rev = db.get(AppRevision, rev_id) if rev_id else None
    if rev is None or rev.application_id != app.id:
        raise api_error(409, "no_revision", "the application has no such revision") if rev_id else api_error(409, "no_revision", "the application has no model yet")
    if body.open_pull_request and g.provider != "github":
        raise api_error(422, "pull_request_unsupported", "pull requests need provider 'github'")
    token = _token(g)
    if body.open_pull_request and token is None:
        raise api_error(422, "token_required", "opening a pull request needs a token reference")
    message = (body.message or f"Orchivio: {app.key} revision {rev.revision_no}") + f"\n\nrevision: {rev.id}\nmodel-hash: {rev.model_hash}\n"
    svc = request.app.state.git_sync
    with _Busy(app.id):
        try:
            result = svc.push(g.remote_url, token, base_branch=g.base_branch, push_branch=g.push_branch, subdir=g.path,
                              files=to_files(to_model(rev)), message=message)
        except GitSyncError as e:
            telemetry.applog(app.id, 40, "gitsync", "git push failed", outcome=e.code)
            raise _fail(e) from e
        pr_url, pr_error = None, None
        if result.changed and body.open_pull_request and token:
            try:
                pr_url = GitHubPullRequests(request.app.state.mcp_transport, request.app.state.settings.git_github_api_url).open(
                    g.remote_url, token, head=g.push_branch, base=g.base_branch, title=f"Orchivio: {app.name} revision {rev.revision_no}",
                    body=f"Model export of `{app.key}` revision {rev.revision_no} (`{rev.model_hash}`).\n\nReview the changed element files; merge to make the "
                         f"change available to Orchivio's *Pull* (it still arrives as a proposal).")
            except GitSyncError as e:
                pr_error = e.message
    if result.changed:
        g.last_push_at, g.last_push_commit, g.last_push_revision_id = utcnow(), result.commit, rev.id
    audit(db, request, "gitsync.pushed", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"revision_no": rev.revision_no, "changed": result.changed, "commit": result.commit, "branch": g.push_branch, "pull_request": pr_url is not None})
    db.commit()
    return {"changed": result.changed, "commit": result.commit, "branch": g.push_branch, "base_branch": g.base_branch, "revision_no": rev.revision_no,
            "pull_request": pr_url, "pull_request_error": pr_error,
            "note": None if result.changed else "the base branch already has exactly this model; nothing was pushed"}


@router.post("/applications/{app_id}/git-sync/pull", status_code=201)
def pull(app_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Any:
    """Read the base branch and propose its model. Never applies anything; a person reviews the proposal as usual."""
    app, project = load_application(db, principal, app_id, "model.propose")
    g = _config(db, app.id)
    token = _token(g)
    svc = request.app.state.git_sync
    with _Busy(app.id):
        try:
            result = svc.pull(g.remote_url, token, branch=g.base_branch, subdir=g.path)
        except GitSyncError as e:
            telemetry.applog(app.id, 40, "gitsync", "git pull failed", outcome=e.code)
            raise _fail(e) from e
    g.last_pull_at, g.last_pull_commit = utcnow(), result.commit
    try:
        model = from_files(result.files)
    except CanonicalError as e:
        db.commit()
        raise api_error(422, "invalid_repository_files", e.message, {"path": e.path}) from e
    try:
        proposal = ProposalRepository(db).create(app.id, model, source="human", title=f"Git sync: {g.base_branch}@{result.commit[:7]}",
                                                 created_by=principal.user_id, organization_id=project.organization_id)
    except ModelInvalid as e:
        db.commit()
        raise api_error(422, "model_invalid", "the model in the repository has validation errors",
                        [i.as_dict() for i in e.issues if i.severity == "error"]) from e
    except NoChanges as e:
        db.commit()
        raise api_error(409, "no_changes", "the repository's model equals the application's current model") from e
    audit(db, request, "gitsync.pulled", actor_id=principal.user_id, org_id=project.organization_id, target_type="application", target_id=app.id,
          meta={"commit": result.commit, "branch": g.base_branch, "proposal_id": str(proposal.id)})
    db.commit()
    return proposal_detail(proposal)

