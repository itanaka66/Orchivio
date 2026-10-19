"""Generated test runs: build the model's runtime, run derived cases, keep the report."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchivio.db import to_model
from orchivio.db.models import AppRevision, Build, TestRun
from orchivio.testgen import run_tests

from ..deps import Principal, api_error, audit, current_principal, get_db, load_revision, utcnow
from ..schemas import Page
from .ai import _guard, run_proposal

router = APIRouter(tags=["tests"])


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: int = Field(default=0, ge=0, le=2**31)
    rows: int = Field(default=20, ge=1, le=100)


def _out(run: TestRun, build: Build, revision_id: uuid.UUID, comparison: dict | None = None) -> dict[str, Any]:
    return {"id": str(run.id), "build_id": str(build.id), "revision_id": str(revision_id), "status": run.status,
            "summary": run.summary, "report_ref": run.report_ref, "created_at": run.created_at.isoformat(),
            "comparison": comparison}


def _report(request: Request, run: TestRun) -> dict[str, Any] | None:
    if not run.report_ref:
        return None
    store = request.app.state.artifacts
    return json.loads(store.get(run.report_ref)) if store.exists(run.report_ref) else None


def _compare(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any] | None:
    if old is None:
        return None
    before = {r["id"]: r["status"] for r in old["results"]}
    after = {r["id"]: r["status"] for r in new["results"]}
    return {
        "newly_failing": sorted(i for i, s in after.items() if s == "failed" and before.get(i) != "failed"),
        "newly_passing": sorted(i for i, s in after.items() if s == "passed" and before.get(i) == "failed"),
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
    }


def _previous_run(db: Session, app_id: uuid.UUID, exclude: uuid.UUID) -> TestRun | None:
    return db.scalar(
        select(TestRun).join(Build, TestRun.build_id == Build.id).join(AppRevision, Build.revision_id == AppRevision.id)
        .where(AppRevision.application_id == app_id, TestRun.id != exclude)
        .order_by(TestRun.created_at.desc(), TestRun.id.desc()).limit(1))


@router.post("/revisions/{revision_id}/test-runs", status_code=201)
def start(revision_id: uuid.UUID, body: RunIn, request: Request, principal: Principal = Depends(current_principal),
          db: Session = Depends(get_db)) -> dict[str, Any]:
    """Runs synchronously (job-queue offload is planned). Uses the preview database engine with a
    temporary runtime that is dropped afterwards; no production data is involved."""
    rev, app, project = load_revision(db, principal, revision_id, "model.propose")
    # Timestamps are set here (microsecond resolution), not by the database default (one second on
    # SQLite), because runs are ordered by them to find "the previous run".
    now = utcnow()
    build = Build(revision_id=rev.id, status="running", created_at=now)
    db.add(build)
    db.flush()
    run = TestRun(build_id=build.id, status="running", created_at=now)
    db.add(run)
    db.flush()
    try:
        report = run_tests(to_model(rev), rev.model_hash, request.app.state.preview_store.engine,
                           seed=body.seed, rows=body.rows)
    except Exception as e:  # noqa: BLE001 - a model that cannot even be built is a failed build
        build.status, run.status = "failed", "failed"
        run.summary = {"error": f"{type(e).__name__}: {e}"[:500]}
        audit(db, request, "tests.run", actor_id=principal.user_id, org_id=project.organization_id,
              target_type="test_run", target_id=run.id, meta={"status": "failed", "build_error": True})
        db.commit()
        return _out(run, build, rev.id)

    data = report.as_dict()
    stored = request.app.state.artifacts.put(f"test-runs/{run.id}.json", json.dumps(data).encode("utf-8"))
    build.status = "succeeded"
    run.report_ref = stored.key
    run.status = "succeeded" if report.ok else "failed"
    run.summary = {"counts": data["counts"], "coverage": data["coverage"], "seed": body.seed, "sha256": stored.sha256,
                   "failed": [r["id"] for r in data["results"] if r["status"] == "failed"][:50]}
    prev = _previous_run(db, app.id, run.id)
    comparison = _compare(_report(request, prev), data) if prev else None
    audit(db, request, "tests.run", actor_id=principal.user_id, org_id=project.organization_id,
          target_type="test_run", target_id=run.id, meta={"status": run.status, **data["counts"]})
    db.commit()
    return _out(run, build, rev.id, comparison)


def _load_run(db: Session, principal: Principal, run_id: uuid.UUID, action: str):
    run = db.get(TestRun, run_id)
    build = db.get(Build, run.build_id) if run else None
    if run is None or build is None:
        raise api_error(404, "not_found", "not found")
    rev, app, project = load_revision(db, principal, build.revision_id, action)
    return run, build, rev, app, project


@router.get("/test-runs/{run_id}")
def get_run(run_id: uuid.UUID, request: Request, report: bool = Query(False),
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    run, build, rev, _, _ = _load_run(db, principal, run_id, "model.read")
    out = _out(run, build, rev.id)
    if report:
        out["report"] = _report(request, run)
    return out


@router.get("/revisions/{revision_id}/test-runs", response_model=Page[dict])
def list_runs(revision_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Page[dict]:
    rev, _, _ = load_revision(db, principal, revision_id, "model.read")
    q = select(TestRun, Build).join(Build, TestRun.build_id == Build.id).where(Build.revision_id == rev.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.execute(q.order_by(TestRun.created_at.desc()).limit(limit).offset(offset)).all()
    return Page(items=[_out(r, b, rev.id) for r, b in rows], limit=limit, offset=offset, total=total)


@router.post("/test-runs/{run_id}/ai/repair", status_code=201)
def propose_repair(run_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    """Turn a failed run into a repair *proposal* (never applied automatically)."""
    run, _, rev, app, project = _load_run(db, principal, run_id, "model.propose")
    report = _report(request, run)
    failures = [r for r in (report or {}).get("results", []) if r["status"] == "failed"]
    if not failures:
        raise api_error(409, "nothing_to_repair", "this run has no failed tests")
    if app.current_revision_id != rev.id:
        raise api_error(409, "stale_run", "the run is for an older revision than the current model")
    provider, _, _ = _guard(request, db, principal, app.id, "model.propose")
    kinds = Counter(r.get("kind", r["id"].split(":")[0]) for r in failures)
    root_causes = [{"kind": k, "count": c} for k, c in kinds.most_common()]
    kind_summary = ", ".join(f"{c} {k}" for k, c in kinds.most_common())
    lines = "\n".join(f"- {r['id']}: {r['title']} -- {r['message']}" for r in failures[:25])
    text = (f"Automated tests generated from the current model failed. Root causes by category: {kind_summary}. "
            "Propose a corrected model that makes them pass while keeping access as restrictive as the requirements allow.\n"
            "Failed tests:\n" + lines)[:20_000]
    out = run_proposal(db, provider, principal, app, project, text, f"Repair {len(failures)} failing test(s)",
                       job_type="repair_model")
    result = out.model_dump(mode="json")
    result["root_causes"] = root_causes
    return result

