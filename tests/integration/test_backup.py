"""Backup and restore (FR-044): a restored system must behave like the original."""

from orchivio.db.schema_revision import SCHEMA_REVISION
import io
import json
import shutil
import zipfile

import pytest
from sqlalchemy import func, insert, select, text
from test_tenancy import add_member, make_org
from tests_support import Api

from orchivio.api import create_app
from orchivio.api.settings import Settings
from orchivio.cli import main
from orchivio.db import make_engine
from orchivio.db.models import Base, Membership
from orchivio.ops import BackupError, backup, read_backup, restore


@pytest.fixture
def system(app, new_api, raw_model, tmp_path):
    """A populated system: users, tenancy, revisions, proposals, a test run (artifact), audit events, tokens."""
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    editor = new_api("editor@example.com")
    add_member(owner, org, "editor@example.com", "editor")
    application = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inv", "model": raw_model}).json()
    changed = json.loads(json.dumps(raw_model))
    changed["modules"][0]["entities"][0]["fields"].append({"name": "note", "type": "string"})
    editor.post(f"/applications/{application['id']}/proposals", json={"model": changed, "title": "add note"})
    run = editor.post(f"/revisions/{application['current_revision_id']}/test-runs", json={}).json()
    token = owner.post("/tokens", json={"name": "ci", "organization_id": org["id"]}).json()["token"]
    return {"owner": owner, "editor": editor, "org": org, "app": application, "run": run, "token": token,
            "engine": app.state.session_factory.kw["bind"], "artifacts": app.state.artifacts.root}


def dump(engine, exclude=("sessions",)):
    with engine.connect() as c:
        return {t.name: sorted(json.dumps(dict(r), sort_keys=True, default=str)
                               for r in c.execute(select(t)).mappings())
                for t in Base.metadata.sorted_tables if t.name not in exclude}


def fresh_database(migrated_template, tmp_path, name="restored.db"):
    path = tmp_path / name
    shutil.copy(migrated_template, path)
    return make_engine(f"sqlite:///{path}"), f"sqlite:///{path}"


def test_a_restored_system_has_exactly_the_same_data(system, migrated_template, tmp_path):
    data = backup(system["engine"], artifacts_dir=system["artifacts"])
    target, _ = fresh_database(migrated_template, tmp_path)
    report = restore(data, target, artifacts_dir=tmp_path / "artifacts-restored")
    assert dump(target) == dump(system["engine"])
    assert report.artifacts >= 1 and report.excluded == ["sessions"]
    assert report.tables["users"] == 2 and report.tables["app_revisions"] == 1 and report.tables["proposals"] == 1
    key = system["run"]["report_ref"]
    assert (tmp_path / "artifacts-restored" / key).read_bytes() == (system["artifacts"] / key).read_bytes()


def test_the_restored_system_works(system, migrated_template, tmp_path):
    data = backup(system["engine"], artifacts_dir=system["artifacts"])
    target, url = fresh_database(migrated_template, tmp_path)
    restore(data, target, artifacts_dir=tmp_path / "art")
    app2 = create_app(Settings(database_url=url, preview_database_url=f"sqlite:///{tmp_path / 'p2.db'}", artifact_dir=str(tmp_path / "art")))
    api = Api(app2)
    api.login("owner@example.com")  # password hashes survived
    assert api.get("/me").json()["organizations"][0]["slug"] == system["org"]["slug"]
    rev = api.get(f"/revisions/{system['app']['current_revision_id']}").json()
    assert rev["model_hash"].startswith("sha256:")
    original = system["owner"].get(f"/revisions/{system['app']['current_revision_id']}").json()
    assert rev["model_hash"] == original["model_hash"] and rev["model_json"] == original["model_json"]
    assert api.get(f"/test-runs/{system['run']['id']}?report=true").json()["report"]["ok"] is True  # artifact restored
    # tokens survive (only their hashes were stored)
    from fastapi.testclient import TestClient
    assert TestClient(app2).get("/api/v1/me", headers={"Authorization": f"Bearer {system['token']}"}).status_code == 200
    # the audit trail is intact and still append-only in meaning: new events continue after the old ones
    events = api.get(f"/organizations/{system['org']['id']}/audit-events?limit=200").json()
    assert events["total"] >= 8
    api.post("/organizations", json={"name": "After", "slug": "after-restore"})
    after = api.get(f"/organizations/{system['org']['id']}/audit-events?limit=200").json()
    assert after["items"][0]["id"] >= events["items"][0]["id"]


def test_sessions_are_excluded_by_default_and_can_be_included(system, migrated_template, tmp_path):
    cookie = system["owner"].c.cookies.get("orchivio_session")
    plain = backup(system["engine"])
    t1, url1 = fresh_database(migrated_template, tmp_path, "a.db")
    restore(plain, t1)
    app = create_app(Settings(database_url=url1, preview_database_url="sqlite:///:memory:"))
    from fastapi.testclient import TestClient
    assert TestClient(app, cookies={"orchivio_session": cookie}).get("/api/v1/me").status_code == 401  # must log in again

    with_sessions = backup(system["engine"], exclude=())
    t2, url2 = fresh_database(migrated_template, tmp_path, "b.db")
    restore(with_sessions, t2)
    app2 = create_app(Settings(database_url=url2, preview_database_url="sqlite:///:memory:"))
    assert TestClient(app2, cookies={"orchivio_session": cookie}).get("/api/v1/me").status_code == 200


def test_restore_refuses_a_database_that_is_not_empty_or_not_at_the_same_revision(system, migrated_template, tmp_path):
    data = backup(system["engine"])
    target, _ = fresh_database(migrated_template, tmp_path)
    with target.begin() as c:
        c.execute(insert(Base.metadata.tables["organizations"]).values(name="x", slug="x"))
    with pytest.raises(BackupError, match="not empty"):
        restore(data, target)
    old, _ = fresh_database(migrated_template, tmp_path, "old.db")
    with old.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '0002'"))
    with pytest.raises(BackupError, match="migrate the target"):
        restore(data, old)


def test_corruption_and_tampering_are_detected(system):
    data = backup(system["engine"], artifacts_dir=system["artifacts"])
    manifest, _ = read_backup(data)
    assert manifest["tables"]["users"]["rows"] == 2 and manifest["schema_revision"] == SCHEMA_REVISION

    def rewrite(change):
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w") as zout:
            entries = {i.filename: zin.read(i) for i in zin.infolist()}
            change(entries)
            for n, b in entries.items():
                zout.writestr(n, b)
        return out.getvalue()

    with pytest.raises(BackupError, match="does not match the manifest"):
        read_backup(rewrite(lambda e: e.update({"tables/users.jsonl": e["tables/users.jsonl"].replace(b"owner@", b"hacker@")})))
    with pytest.raises(BackupError, match="missing"):
        read_backup(rewrite(lambda e: e.pop("tables/projects.jsonl")))
    with pytest.raises(BackupError, match="unexpected file"):
        read_backup(rewrite(lambda e: e.update({"extra.txt": b"x"})))
    with pytest.raises(BackupError, match="manifest is missing"):
        read_backup(rewrite(lambda e: e.pop("manifest.json")))
    with pytest.raises(BackupError, match="not a valid"):
        read_backup(b"nope")


def test_a_failed_restore_changes_nothing(system, migrated_template, tmp_path):
    data = backup(system["engine"])
    # a row that violates a foreign key, with the manifest hash fixed up so only the database objects
    import hashlib

    manifest, files = read_backup(data)
    bad = json.loads(files["tables/memberships.jsonl"].decode().splitlines()[0])
    bad["organization_id"] = "00000000-0000-4000-8000-000000000999"
    files["tables/memberships.jsonl"] = (json.dumps(bad) + "\n").encode()
    manifest["tables"]["memberships"] = {"rows": 1, "sha256": hashlib.sha256(files["tables/memberships.jsonl"]).hexdigest()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for n, b in files.items():
            z.writestr(n, b)
    target, _ = fresh_database(migrated_template, tmp_path)
    with pytest.raises(Exception):
        restore(out.getvalue(), target, artifacts_dir=tmp_path / "never")
    with target.connect() as c:
        assert all(c.execute(select(func.count()).select_from(t)).scalar_one() == 0 for t in Base.metadata.sorted_tables)
    assert not (tmp_path / "never").exists()  # artifacts are written only after the database commit
    assert Membership.__tablename__ == "memberships"


def test_the_cli_round_trip(system, migrated_template, tmp_path, capsys):
    archive = tmp_path / "b.zip"
    url = str(system["engine"].url)
    assert main(["backup", str(archive), "--database-url", url, "--artifacts", str(system["artifacts"])]) == 0
    assert main(["verify-backup", str(archive)]) == 0 and "tables" in capsys.readouterr().out
    target, target_url = fresh_database(migrated_template, tmp_path)
    assert main(["restore", str(archive), "--database-url", target_url, "--artifacts", str(tmp_path / "ra")]) == 0
    assert dump(target) == dump(system["engine"])
    assert main(["restore", str(archive), "--database-url", target_url]) == 1  # now it is not empty
    assert "not empty" in capsys.readouterr().err
    assert main(["verify-backup", str(tmp_path / "missing.zip")]) == 2
