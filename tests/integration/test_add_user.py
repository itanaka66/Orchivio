"""Creating Studio users from the command line and from scripts/add-user.sh|bat."""

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from orchivio.auth import verify_password
from orchivio.cli import main
from orchivio.db import make_engine, make_session_factory
from orchivio.db.models import AuditEvent, Base, Membership, Organization, User
from orchivio.ops import UserError, add_user

ROOT = Path(__file__).resolve().parents[2]
SRC = str(ROOT / "src")
GOOD = "a long passphrase 42"


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'p.db').as_posix()}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as s:
        s.add(Organization(name="Acme", slug="acme"))
        s.commit()
    return url, sessions


def users(sessions):
    with sessions() as s:
        return list(s.scalars(select(User).order_by(User.email)))


# --- the operation ---------------------------------------------------------------------------------------


def test_add_user_with_membership_and_audit(db):
    url, sessions = db
    with sessions() as s:
        r = add_user(s, email="  Ann@Example.COM ", display_name=" Ann Lee ", password=GOOD, org_slug="acme", role="editor")
    assert (r.email, r.organization, r.role) == ("ann@example.com", "acme", "editor")
    [u] = users(sessions)
    assert u.email == "ann@example.com" and u.display_name == "Ann Lee" and u.password_hash != GOOD and verify_password(GOOD, u.password_hash)
    with sessions() as s:
        m = s.scalar(select(Membership).where(Membership.user_id == u.id))
        assert m.role == "editor"
        event = s.scalar(select(AuditEvent).where(AuditEvent.action == "user.created_cli"))
        assert event.target_id == str(u.id) and event.organization_id == m.organization_id and GOOD not in str(event.meta)


def test_add_user_without_an_organization(db):
    _, sessions = db
    with sessions() as s:
        assert add_user(s, email="solo@example.com", display_name="Solo", password=GOOD).organization is None
    with sessions() as s:
        assert s.scalar(select(Membership)) is None


@pytest.mark.parametrize("kw,fragment", [
    ({"email": "not-an-email"}, "e-mail"), ({"email": "a b@example.com"}, "e-mail"), ({"display_name": "   "}, "name"), ({"display_name": "x" * 201}, "name"),
    ({"password": "short"}, "at least"), ({"password": "aaaaaaaaaaaaaaaa"}, "repetitive"), ({"org_slug": "acme"}, "both"), ({"role": "viewer"}, "both"),
    ({"org_slug": "nope", "role": "viewer"}, "no organization"), ({"org_slug": "acme", "role": "emperor"}, "role must be"),
])
def test_bad_input_changes_nothing(db, kw, fragment):
    _, sessions = db
    args = {"email": "ann@example.com", "display_name": "Ann", "password": GOOD, **kw}
    with sessions() as s, pytest.raises(UserError, match=fragment):
        add_user(s, **args)
    assert users(sessions) == []


def test_duplicate_emails_are_refused_case_insensitively(db):
    _, sessions = db
    with sessions() as s:
        add_user(s, email="ann@example.com", display_name="Ann", password=GOOD)
    with sessions() as s, pytest.raises(UserError, match="already exists"):
        add_user(s, email="ANN@example.com", display_name="Other", password=GOOD)
    assert len(users(sessions)) == 1


# --- the CLI ---------------------------------------------------------------------------------------------


def run_cli(url, *args, stdin=None):
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.run([sys.executable, "-m", "orchivio.cli", "user", "add", "--database-url", url, *args], input=stdin, capture_output=True, text=True, env=env)


def test_cli_reads_the_password_from_stdin(db):
    url, sessions = db
    p = run_cli(url, "--email", "bob@example.com", "--name", "Bob", "--org", "acme", "--role", "viewer", "--password-stdin", stdin=GOOD + "\n")
    assert p.returncode == 0, p.stderr
    assert "created bob@example.com" in p.stdout and "in 'acme' as viewer" in p.stdout and GOOD not in p.stdout + p.stderr
    assert verify_password(GOOD, users(sessions)[0].password_hash)


def test_cli_refuses_when_it_cannot_ask_for_a_password(db):
    url, sessions = db
    p = run_cli(url, "--email", "bob@example.com", "--name", "Bob", stdin="")
    assert p.returncode == 2 and "--password-stdin" in p.stderr and users(sessions) == []


def test_cli_reports_problems_and_exits_nonzero(db):
    url, sessions = db
    p = run_cli(url, "--email", "bob@example.com", "--name", "Bob", "--password-stdin", stdin="short\n")
    assert p.returncode == 1 and "at least" in p.stderr and users(sessions) == []
    assert run_cli(url, "--email", "bob@example.com", "--name", "Bob", "--org", "acme", "--password-stdin", stdin=GOOD + "\n").returncode == 1


def test_there_is_no_password_argument():
    out = io.StringIO()
    with pytest.raises(SystemExit):
        main(["user", "add", "--email", "a@b.c", "--name", "A", "--password", "x"], out)


def test_the_new_user_can_sign_in(app):
    from fastapi.testclient import TestClient

    with app.state.session_factory() as s:
        add_user(s, email="cli@example.com", display_name="Cli", password=GOOD)
    resp = TestClient(app, base_url="https://testserver").post("/api/v1/auth/login", json={"email": "cli@example.com", "password": GOOD})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == "cli@example.com"


# --- the scripts -------------------------------------------------------------------------------------------


def script_env(url, password=GOOD, posix=False):
    exe = Path(sys.executable).as_posix() if posix else sys.executable
    if " " in exe:
        pytest.skip("ORCHIVIO_BIN is split on spaces, so this interpreter path cannot be used")
    return {**os.environ, "PYTHONPATH": SRC, "ORCHIVIO_DATABASE_URL": url, "ORCHIVIO_BIN": f"{exe} -m orchivio.cli",
            **({"ORCHIVIO_NEW_PASSWORD": password} if password else {})}


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_sh_script_with_flags_and_environment_password(db):
    url, sessions = db
    sh = ROOT / "scripts" / "add-user.sh"
    p = subprocess.run(["sh", str(sh), "-e", "sh@example.com", "-n", "Sh User", "-o", "acme", "-r", "admin"], capture_output=True, text=True, env=script_env(url, posix=True), stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "created sh@example.com" in p.stdout
    assert verify_password(GOOD, users(sessions)[0].password_hash)


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_sh_script_with_password_on_stdin_and_odd_names(db):
    url, sessions = db
    sh = ROOT / "scripts" / "add-user.sh"
    env = script_env(url, password=None, posix=True)
    p = subprocess.run(["sh", str(sh), "-e", "odd@example.com", "-n", "Zoë O'Neil \"Z\"", "-p"], input="pässwörd with spaces 99\n", capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    [u] = users(sessions)
    assert u.display_name == "Zoë O'Neil \"Z\"" and verify_password("pässwörd with spaces 99", u.password_hash)


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_sh_script_validates_its_arguments_and_never_accepts_a_password_flag(db):
    url, sessions = db
    sh = ROOT / "scripts" / "add-user.sh"
    env = script_env(url, posix=True)
    run = lambda *a: subprocess.run(["sh", str(sh), *a], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)  # noqa: E731
    assert run("-e", "x@example.com", "-n", "X", "-o", "acme").returncode == 2  # org without role
    assert run("-e", "x@example.com", "-n", "X", "-r", "viewer").returncode == 2
    assert run("-e", "x@example.com").returncode == 2  # no name and nothing to ask on
    assert run("--password", "secret").returncode == 2
    no_pw = subprocess.run(["sh", str(sh), "-e", "x@example.com", "-n", "X"], capture_output=True, text=True, env=script_env(url, password=None, posix=True), stdin=subprocess.DEVNULL)
    assert no_pw.returncode == 2 and "no terminal" in no_pw.stderr
    assert users(sessions) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch file")
def test_bat_script_creates_a_user(db):
    url, sessions = db
    bat = str(ROOT / "scripts" / "add-user.bat")
    p = subprocess.run(["cmd", "/c", bat, "bat@example.com", "Bat User", "acme", "editor"], capture_output=True, text=True, env=script_env(url), stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "created bat@example.com" in p.stdout
    [u] = users(sessions)
    assert verify_password(GOOD, u.password_hash)
    with sessions() as s:
        assert s.scalar(select(Membership)).role == "editor"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch file")
def test_bat_script_survives_special_characters_and_validates(db):
    url, sessions = db
    bat = str(ROOT / "scripts" / "add-user.bat")
    env = script_env(url, password="p&ss | word ^ 100% !ok")
    p = subprocess.run(["cmd", "/c", bat, "weird@example.com", "A & B | C ^ 50%"], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    [u] = users(sessions)
    assert u.display_name == "A & B | C ^ 50%" and verify_password("p&ss | word ^ 100% !ok", u.password_hash)
    bad = subprocess.run(["cmd", "/c", bat, "x@example.com", "X", "acme"], capture_output=True, text=True, env=script_env(url), stdin=subprocess.DEVNULL)
    assert bad.returncode == 2 and "both an organization and a role" in bad.stderr
    assert len(users(sessions)) == 1
