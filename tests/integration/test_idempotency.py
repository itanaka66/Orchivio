"""Idempotency keys: a retried creation does not create twice (docs/OFFLINE.md)."""

import copy
import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_tenancy import add_member, make_org

from orchivio.api.deps import utcnow
from orchivio.db.models import IdempotencyKey, Requirement, Story

KEY = "k-0123456789"


@pytest.fixture
def world(new_api, raw_model):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    app = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inventory", "model": raw_model}).json()
    return proj, app, owner, editor


def count(app, model, **where):
    with app.state.session_factory() as s:
        return s.scalar(select(func.count()).select_from(model).where(*[getattr(model, k) == v for k, v in where.items()]))


def post(c, proj, body, key=KEY, path="requirements"):
    return c.post(f"/projects/{proj['id']}/{path}", json=body, headers={"Idempotency-Key": key} if key else {})


def test_a_retry_returns_the_first_answer_and_creates_nothing_new(app, world):
    proj, _, _, editor = world
    first = post(editor, proj, {"title": "Track stock"})
    again = post(editor, proj, {"title": "Track stock"})
    assert first.status_code == 201 and again.status_code == 201
    assert again.content == first.content and again.headers["idempotency-replayed"] == "true" and "idempotency-replayed" not in first.headers
    assert count(app, Requirement) == 1
    third = post(editor, proj, {"title": "Track stock"})
    assert third.content == first.content


def test_without_the_header_nothing_changes(app, world):
    proj, _, _, editor = world
    a, b = post(editor, proj, {"title": "x"}, key=None), post(editor, proj, {"title": "x"}, key=None)
    assert a.status_code == b.status_code == 201 and a.json()["id"] != b.json()["id"]


def test_a_key_names_one_request(app, world):
    proj, _, _, editor = world
    assert post(editor, proj, {"title": "one"}).status_code == 201
    other_body = post(editor, proj, {"title": "two"})
    assert other_body.status_code == 422 and other_body.json()["error"]["code"] == "idempotency_key_reuse"
    other_path = post(editor, proj, {"title": "two"}, path="stories")
    assert other_path.status_code == 422 and other_path.json()["error"]["code"] == "idempotency_key_reuse"
    assert count(app, Requirement) == 1 and count(app, Story) == 0


@pytest.mark.parametrize("key", ["short", "x" * 65, "has space 123", "semi;colon-123", "ünicode-1234"])
def test_malformed_keys_are_refused(world, key):
    proj, _, _, editor = world
    try:
        r = post(editor, proj, {"title": "x"}, key=key)
    except UnicodeEncodeError:
        return  # not even representable as an HTTP header
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_idempotency_key"


def test_keys_belong_to_the_user_who_used_them(app, world, new_api):
    proj, _, owner, editor = world
    a = post(editor, proj, {"title": "mine"})
    b = post(owner, proj, {"title": "theirs"})  # the same key, another user: independent
    assert a.status_code == b.status_code == 201 and a.json()["id"] != b.json()["id"] and "idempotency-replayed" not in b.headers
    assert count(app, IdempotencyKey) == 2


def test_failures_are_not_stored_so_a_corrected_request_can_use_the_key(app, world):
    proj, _, _, editor = world
    bad = post(editor, proj, {"title": ""})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    assert count(app, IdempotencyKey) == 0
    good = post(editor, proj, {"title": "fixed"})
    assert good.status_code == 201 and count(app, Requirement) == 1


def test_authentication_is_still_checked_first(app, world):
    proj, _, _, editor = world
    from fastapi.testclient import TestClient

    anonymous = TestClient(app).post(f"/api/v1/projects/{proj['id']}/requirements", json={"title": "x"}, headers={"Idempotency-Key": KEY})
    assert anonymous.status_code == 401
    post(editor, proj, {"title": "stored"})
    stranger = TestClient(app)
    replay_as_stranger = stranger.post(f"/api/v1/projects/{proj['id']}/requirements", json={"title": "stored"}, headers={"Idempotency-Key": KEY})
    assert replay_as_stranger.status_code == 401  # knowing the key is not enough
    editor.csrf = None
    no_csrf = post(editor, proj, {"title": "stored"})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "csrf_failed"  # a replay does not bypass CSRF protection


def test_a_request_still_running_is_reported(app, world):
    import hashlib

    from orchivio.db.models import User

    proj, _, _, editor = world
    with app.state.session_factory() as s:
        uid = s.scalar(select(User.id).where(User.email == "editor@example.com"))
        digest = hashlib.sha256(f"POST\n/api/v1/projects/{proj['id']}/requirements\n".encode() + b'{"title":"slow"}').hexdigest()
        s.add(IdempotencyKey(user_id=uid, key=KEY, request_hash=digest, state="running"))
        s.commit()
    r = editor.post(f"/projects/{proj['id']}/requirements", content=b'{"title":"slow"}', headers={"Idempotency-Key": KEY, "Content-Type": "application/json"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "request_in_progress" and r.headers["retry-after"] == "1"
    assert count(app, Requirement) == 0


def test_keys_expire_after_a_day(app, world):
    proj, _, _, editor = world
    first = post(editor, proj, {"title": "old"})
    with app.state.session_factory() as s:
        for row in s.scalars(select(IdempotencyKey)):
            row.created_at = utcnow() - timedelta(hours=25)
        s.commit()
    second = post(editor, proj, {"title": "old"})
    assert second.status_code == 201 and second.json()["id"] != first.json()["id"] and "idempotency-replayed" not in second.headers


def test_the_other_creation_endpoints_take_part(app, world, raw_model):
    proj, application, owner, editor = world
    s1, s2 = post(editor, proj, {"title": "A story"}, key="story-key-1", path="stories"), post(editor, proj, {"title": "A story"}, key="story-key-1", path="stories")
    assert s1.content == s2.content and s2.headers["idempotency-replayed"] == "true" and count(app, Story) == 1
    f = lambda: editor.post(f"/applications/{application['id']}/feedback", json={"page_key": "p", "body": "hello"}, headers={"Idempotency-Key": "feedback-key-1"})  # noqa: E731
    assert f().content == f().content
    with app.state.session_factory() as s:
        from orchivio.db.models import FeedbackItem

        assert s.scalar(select(func.count()).select_from(FeedbackItem)) == 1
    changed = copy.deepcopy(raw_model)
    changed["modules"][0]["entities"][0]["display_name"] = "Renamed"
    p = lambda: editor.post(f"/applications/{application['id']}/proposals", json={"model": changed, "title": "Rename"}, headers={"Idempotency-Key": "proposal-key-1"})  # noqa: E731
    one, two = p(), p()
    assert one.status_code == 201 and one.content == two.content and two.headers["idempotency-replayed"] == "true"
    assert editor.get(f"/applications/{application['id']}/proposals").json()["total"] == 1


def test_concurrent_identical_requests_create_one_record(app, world):
    proj, _, _, editor = world
    results = []

    def send():
        results.append(post(editor, proj, {"title": "race"}))

    threads = [threading.Thread(target=send) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(r.status_code for r in results)
    assert set(statuses) <= {201, 409} and 201 in statuses
    assert count(app, Requirement) == 1
    final = post(editor, proj, {"title": "race"})
    assert final.status_code == 201 and final.headers.get("idempotency-replayed") == "true"
