"""Application log lines, the viewer API and AI log analysis (FR-123)."""

import json
import logging

import pytest
from test_ai_api import ctx, jobs, use  # noqa: F401 - fixtures and helpers reused
from test_jobs import Clock, seed_low_stock, with_schedule
from test_jobs import ctx as jobs_ctx  # noqa: F401
from test_jobs import make_app as make_job_app

from orchivio import telemetry
from orchivio.jobs import Worker


def put(app_id, level, message, source="job", **fields):
    telemetry.applog(app_id, level, source, message, **fields)


def get(c, a, **q):
    r = c.get(f"/applications/{a['id']}/logs", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def test_background_jobs_write_log_lines(jobs_ctx, app):  # noqa: F811
    proj, p, raw = jobs_ctx
    a = make_job_app(p["owner"], proj, with_schedule(raw))
    seed_low_stock(p["owner"], a)
    clock = Clock()
    w = Worker(app.state.session_factory, app.state.preview_store, audit=app.state.audit_sink, clock=clock)
    w.tick()
    clock.advance(minutes=5)
    assert w.tick().succeeded == 1
    items = get(p["owner"], a)["items"]
    # the engine now also writes a "flow run" line (source=flow); the worker still writes "job succeeded"
    [line] = [x for x in items if x["source"] == "job"]
    assert (line["level"], line["message"]) == ("info", "job succeeded")
    assert line["fields"] == {"flow": "core.ReorderLowStock", "trigger": "schedule", "outcome": "succeeded", "attempt": 1}
    [flow_line] = [x for x in items if x["source"] == "flow"]
    assert flow_line["message"] == "flow run" and flow_line["fields"]["outcome"] == "succeeded"


def test_viewer_filters_paging_and_ordering(ctx):  # noqa: F811
    proj, a, p = ctx
    for i in range(5):
        put(a["id"], logging.ERROR if i == 4 else logging.INFO, f"event {i}", source="webhook" if i % 2 else "job")
    put(a["id"], logging.WARNING, "100% done_ok")
    ed = p["editor"]
    assert [x["message"] for x in get(ed, a)["items"]] == ["100% done_ok", "event 4", "event 3", "event 2", "event 1", "event 0"]
    assert [x["message"] for x in get(ed, a, level="warning")["items"]] == ["100% done_ok", "event 4"]
    assert {x["source"] for x in get(ed, a, source="webhook")["items"]} == {"webhook"}
    assert [x["message"] for x in get(ed, a, q="event 3")["items"]] == ["event 3"]
    assert [x["message"] for x in get(ed, a, q="%")["items"]] == ["100% done_ok"]  # literal, not a wildcard
    first = get(ed, a, limit=4)
    second = get(ed, a, limit=4, before_id=first["next_before_id"])
    assert len(first["items"]) == 4 and first["next_before_id"] and len(second["items"]) == 2 and second["next_before_id"] is None


def test_logs_need_editor_and_stay_in_their_application(ctx):  # noqa: F811
    proj, a, p = ctx
    b = p["owner"].post(f"/projects/{proj['id']}/applications", json={"key": "other", "name": "Other"}).json()
    put(a["id"], logging.INFO, "mine")
    put(b["id"], logging.INFO, "theirs")
    assert [x["message"] for x in get(p["editor"], a)["items"]] == ["mine"]
    assert p["viewer"].get(f"/applications/{a['id']}/logs").status_code == 403
    outsider = p["owner"]  # same org, other application is allowed; unknown ids are 404
    assert outsider.get("/applications/00000000-0000-0000-0000-000000000000/logs").status_code == 404
    assert p["viewer"].post(f"/applications/{a['id']}/logs/analyze", json={}).status_code == 403


def test_only_allow_listed_scalar_fields_are_stored(ctx):  # noqa: F811
    proj, a, p = ctx
    put(a["id"], logging.INFO, "x", flow="core.F", token="s3cret", payload={"a": 1}, status=204)
    [line] = get(p["editor"], a)["items"]
    assert line["fields"] == {"flow": "core.F", "status": 204}


def test_analysis_cites_only_lines_it_was_given(app, ctx):  # noqa: F811
    proj, a, p = ctx
    put(a["id"], logging.ERROR, "job dead", flow="core.F", outcome="dead", attempt=5)
    put(a["id"], logging.WARNING, "webhook delivery retry", source="webhook", outcome="retry", status=503)
    ids = [x["id"] for x in get(p["editor"], a)["items"]]
    provider = use(app, {"summary": "A flow keeps failing and a receiver is down.", "findings": [
        {"title": "Receiver down", "severity": "medium", "explanation": "503s", "suggestion": "check it", "evidence_ids": [ids[0], 9999]},
        {"title": "Invented", "severity": "high", "explanation": "x", "evidence_ids": [9999]}]})
    r = p["editor"].post(f"/applications/{a['id']}/logs/analyze", json={"minutes": 30, "question": "why?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lines_analyzed"] == 2 and body["dropped_findings"] == 1
    assert [f["title"] for f in body["findings"]] == ["Receiver down"] and body["findings"][0]["evidence_ids"] == [ids[0]]
    sent = json.dumps([m.content for m in provider.requests[0].messages])
    assert "<logs>" in sent and "job dead" in sent
    [job] = jobs(app)
    assert (job.type, job.status) == ("analyze_logs", "succeeded") and job.prompt_template_version == "analyze_logs@1"


def test_analysis_without_lines_or_provider_or_consent(app, ctx):  # noqa: F811
    proj, a, p = ctx
    ed = p["editor"]
    assert ed.post(f"/applications/{a['id']}/logs/analyze", json={}).json()["error"]["code"] == "ai_not_configured"
    use(app, {"summary": "-", "findings": []}, external=True)
    assert ed.post(f"/applications/{a['id']}/logs/analyze", json={}).json()["error"]["code"] == "external_ai_disabled"
    p["owner"].patch(f"/projects/{proj['id']}", json={"settings": {"ai_allow_external": True}})
    r = ed.post(f"/applications/{a['id']}/logs/analyze", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_logs"
    assert jobs(app) == []  # nothing was sent, nothing was recorded


def test_analysis_rejects_unusable_output(app, ctx):  # noqa: F811
    proj, a, p = ctx
    put(a["id"], logging.ERROR, "boom")
    use(app, "not json", "still not json", "nope")
    r = p["editor"].post(f"/applications/{a['id']}/logs/analyze", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ai_output_invalid"


@pytest.mark.parametrize("body", [{"minutes": 0}, {"minutes": 5000}, {"level": "fatal"}, {"extra": 1}])
def test_analysis_validates_its_input(ctx, body):  # noqa: F811
    proj, a, p = ctx
    assert p["editor"].post(f"/applications/{a['id']}/logs/analyze", json=body).status_code == 422
