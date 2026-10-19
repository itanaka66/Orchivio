"""Optimistic concurrency for requirements and stories (If-Match; docs/OFFLINE.md)."""

import pytest
from test_idempotency import world  # noqa: F401  (fixture)


def make(c, proj, kind, **body):
    return c.post(f"/projects/{proj['id']}/{kind}", json={"title": "A", **body}).json()


@pytest.mark.parametrize("kind", ["stories", "requirements"])
def test_versions_count_changes(world, kind):  # noqa: F811
    proj, _, owner, _ = world
    item = make(owner, proj, kind)
    assert item["version"] == 1
    r = owner.patch(f"/projects/{proj['id']}/{kind}/{item['id']}", json={"title": "B"})
    assert r.json()["version"] == 2


@pytest.mark.parametrize("kind", ["stories", "requirements"])
def test_a_change_to_a_stale_version_is_refused_with_the_current_item(world, kind):  # noqa: F811
    proj, _, owner, editor = world
    item = make(owner, proj, kind)
    url = f"/projects/{proj['id']}/{kind}/{item['id']}"
    assert editor.patch(url, json={"title": "Mine"}, headers={"If-Match": '"1"'}).status_code == 200
    stale = owner.patch(url, json={"title": "Theirs"}, headers={"If-Match": '"1"'})
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "version_conflict" and err["details"]["current"]["title"] == "Mine" and err["details"]["current"]["version"] == 2
    assert owner.get(url).json()["title"] == "Mine"  # nothing was overwritten
    assert owner.delete(url, headers={"If-Match": '"1"'}).status_code == 409
    assert owner.get(url).status_code == 200  # nor deleted
    assert owner.patch(url, json={"title": "Theirs"}, headers={"If-Match": '"2"'}).status_code == 200
    assert owner.delete(url, headers={"If-Match": '"3"'}).status_code == 204


def test_without_if_match_the_last_write_wins_and_a_bad_header_is_refused(world):  # noqa: F811
    proj, _, owner, _ = world
    item = make(owner, proj, "stories")
    url = f"/projects/{proj['id']}/stories/{item['id']}"
    assert owner.patch(url, json={"title": "X"}).status_code == 200
    assert owner.patch(url, json={"title": "Y"}, headers={"If-Match": "banana"}).status_code == 400
