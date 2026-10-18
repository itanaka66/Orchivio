import copy
import uuid

import pytest

from orchivio.db import (
    ModelInvalid,
    NotFound,
    RevisionRepository,
    make_engine,
    make_session_factory,
)
from orchivio.db.models import Application, AppRevision, AuditEvent, Organization, Project, User
from orchivio.model import AppModel, migrate_to_latest
from tests_support import load_yaml


@pytest.fixture
def session(db_url):
    engine = make_engine(db_url)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


@pytest.fixture
def app_id(session):
    user = User(email="a@example.com", display_name="A")
    org = Organization(name="Org", slug="org")
    session.add_all([user, org])
    session.flush()
    proj = Project(organization_id=org.id, name="P", slug="p")
    session.add(proj)
    session.flush()
    app = Application(project_id=proj.id, key="inventory", name="Inventory")
    session.add(app)
    session.flush()
    session.info["user"] = user.id
    session.info["org"] = org.id
    return app.id


def model_from(raw) -> AppModel:
    return AppModel.model_validate(migrate_to_latest(raw))


def test_revisions_chain_with_parents_and_audit(session, app_id):
    repo = RevisionRepository(session)
    uid = session.info["user"]
    m = model_from(load_yaml("model.yaml"))
    r1 = repo.create(app_id, m, created_by=uid, message="first", organization_id=session.info["org"])
    assert (r1.revision_no, r1.parent_revision_ids) == (1, [])

    raw2 = load_yaml("model.yaml")
    raw2["modules"][0]["entities"][0]["fields"].append({"name": "note", "type": "string"})
    r2 = repo.create(app_id, model_from(raw2), created_by=uid)
    assert (r2.revision_no, r2.parent_revision_ids) == (2, [r1.id])

    app = session.get(Application, app_id)
    assert app.current_revision_id == r2.id
    assert [r.revision_no for r in repo.list(app_id)] == [2, 1]
    actions = [e.action for e in session.query(AuditEvent).order_by(AuditEvent.id)]
    assert actions == ["revision.created", "revision.created"]


def test_element_ids_are_stable_across_revisions(session, app_id):
    repo = RevisionRepository(session)
    r1 = repo.create(app_id, model_from(load_yaml("model.yaml")), created_by=None)
    raw2 = load_yaml("model.yaml")
    raw2["modules"][0]["entities"][0]["fields"][3]["minimum"] = 1
    r2 = repo.create(app_id, model_from(raw2), created_by=None)
    d = repo.diff(r2.id, r1.id)
    assert [(c.kind, c.element_type, c.name) for c in d.changes] == [
        ("modified", "field", "core.Product.quantity")
    ]


def test_invalid_model_writes_nothing(session, app_id):
    repo = RevisionRepository(session)
    raw = load_yaml("model.yaml")
    raw["security"]["access"] = []
    with pytest.raises(ModelInvalid) as e:
        repo.create(app_id, model_from(raw), created_by=None)
    assert any(i.code == "no-access-rule" for i in e.value.issues)
    assert session.query(AppRevision).count() == 0
    assert session.query(AuditEvent).count() == 0


def test_rollback_creates_new_revision_and_keeps_history(session, app_id):
    repo = RevisionRepository(session)
    r1 = repo.create(app_id, model_from(load_yaml("model.yaml")), created_by=None)
    raw2 = copy.deepcopy(load_yaml("model.yaml"))
    raw2["application"]["name"] = "Renamed"
    r2 = repo.create(app_id, model_from(raw2), created_by=None)
    r3 = repo.rollback(app_id, r1.id, created_by=None)
    assert r3.revision_no == 3 and r3.parent_revision_ids == [r2.id]
    assert r3.model_hash == r1.model_hash
    assert r2.model_json["application"]["name"] == "Renamed"  # history untouched
    assert "revision.rolled_back" in {e.action for e in session.query(AuditEvent)}


def test_rollback_rejects_foreign_revision(session, app_id):
    repo = RevisionRepository(session)
    r1 = repo.create(app_id, model_from(load_yaml("model.yaml")), created_by=None)
    with pytest.raises(NotFound):
        repo.rollback(uuid.uuid4(), r1.id, created_by=None)
    other = Application(project_id=session.get(Application, app_id).project_id, key="other", name="O")
    session.add(other)
    session.flush()
    with pytest.raises(NotFound):
        repo.rollback(other.id, r1.id, created_by=None)


def test_unique_revision_number_per_application(session, app_id):
    from sqlalchemy.exc import IntegrityError

    repo = RevisionRepository(session)
    r1 = repo.create(app_id, model_from(load_yaml("model.yaml")), created_by=None)
    session.add(
        AppRevision(
            application_id=app_id, revision_no=1, schema_version="1.1", model_json={}, model_hash="x"
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    assert r1.revision_no == 1


def test_list_pagination_is_bounded(session, app_id):
    repo = RevisionRepository(session)
    repo.create(app_id, model_from(load_yaml("model.yaml")), created_by=None)
    assert len(repo.list(app_id, limit=10_000)) == 1
    assert repo.list(app_id, limit=0, offset=-5) != []
