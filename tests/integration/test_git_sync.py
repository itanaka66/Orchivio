"""Git synchronisation: push as a branch (and pull request), pull as a proposal, and the hardening around an untrusted remote."""

import copy
import os
import shutil
import subprocess

import pytest
from test_tenancy import add_member, make_org

from orchivio.gitsync import GitSyncError, GitSyncService, PushResult, valid_branch, valid_subdir
from orchivio.gitsync.github import GitHubPullRequests
from orchivio.integrations.transport import Response
from orchivio.model import AppModel, to_files

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(*args, cwd, input=None):
    p = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=ENV, input=input, capture_output=True, check=True)
    return p.stdout.decode("utf-8", "replace").strip()


@pytest.fixture
def remote(tmp_path):
    """A bare repository whose main branch holds a README, plus a helper to commit files to a branch of it."""
    bare = tmp_path / "remote.git"
    git("init", "--bare", "-q", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "seed"
    git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    (work / "README.md").write_text("hello\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    git("push", "-q", str(bare), "main", cwd=work)

    class R:
        path = bare
        url = bare.as_uri()

        def tree(self, ref):
            return git("ls-tree", "-r", "--name-only", ref, cwd=bare).splitlines()

        def read(self, ref, name):
            return git("show", f"{ref}:{name}", cwd=bare)

        def log(self, ref):
            return git("log", "--format=%s", ref, cwd=bare).splitlines()

        def commit_files(self, files, *, branch="main", subdir="", message="edit", remove_all_under=None, extra=None):
            clone = tmp_path / f"c{len(list(tmp_path.iterdir()))}"
            git("clone", "-q", "--branch", branch, str(bare), str(clone), cwd=tmp_path)
            if remove_all_under is not None:
                shutil.rmtree(clone / remove_all_under, ignore_errors=True)
            for name, text in files.items():
                f = clone / subdir / name
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8", newline="\n")
            git("add", "-A", cwd=clone)
            if extra:
                extra(clone)
            git("commit", "-q", "-m", message, cwd=clone)
            git("push", "-q", "origin", branch, cwd=clone)

    return R()


@pytest.fixture
def world(new_api, raw_model, app, remote):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "Inv", "slug": "inventory"}).json()
    people = {"owner": owner}
    for role in ("admin", "editor", "viewer"):
        people[role] = new_api(f"{role}@example.com")
        add_member(owner, org, f"{role}@example.com", role)
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inventory", "name": "Inventory", "model": raw_model}).json()
    app.state.git_sync = GitSyncService(allow_local=True)
    return a, people


def configure(c, a, remote, **kw):
    return c.put(f"/applications/{a['id']}/git-sync", json={"remote_url": remote.url, **kw})


def push(c, a, **kw):
    return c.post(f"/applications/{a['id']}/git-sync/push", json=kw)


def pull(c, a):
    return c.post(f"/applications/{a['id']}/git-sync/pull")


def model_files(raw_model, edit=None):
    m = copy.deepcopy(raw_model)
    if edit:
        edit(m)
    return to_files(AppModel.model_validate(m))


# --- configuration -----------------------------------------------------------------------------------


def test_configuration_needs_an_admin_and_is_off_by_default(app, new_api, raw_model, remote):
    owner = new_api("owner@example.com")
    org = make_org(owner)
    proj = owner.post(f"/organizations/{org['id']}/projects", json={"name": "I", "slug": "inv"}).json()
    a = owner.post(f"/projects/{proj['id']}/applications", json={"key": "inv", "name": "I", "model": raw_model}).json()
    r = configure(owner, a, remote)
    assert r.status_code == 409 and r.json()["error"]["code"] == "git_sync_disabled"  # no allowed hosts configured on this server


def test_configuration_is_validated(world, app, remote):
    a, p = world
    assert configure(p["editor"], a, remote).status_code == 403
    assert configure(p["admin"], a, remote).status_code == 200
    ok = configure(p["admin"], a, remote, path="models/inv", token_ref="env:GIT_TOKEN").json()
    assert ok["push_branch"] == "orchivio/inventory" and ok["path"] == "models/inv" and ok["token_ref"] == "env:GIT_TOKEN"
    for bad in ({"push_branch": "main"}, {"base_branch": "--upload-pack=x"}, {"base_branch": "a..b"}, {"push_branch": "x.lock"}, {"path": "../etc"},
                {"path": "a/.git/b"}, {"path": "/abs"}, {"token_ref": "vault:secret"}, {"provider": "svn"}):
        assert configure(p["admin"], a, remote, **bad).status_code == 422, bad
    assert p["editor"].get(f"/applications/{a['id']}/git-sync").status_code == 403
    assert p["admin"].get(f"/applications/{a['id']}/git-sync").json()["remote_url"] == remote.url


def test_remote_urls_are_restricted_to_allowed_https_hosts():
    svc = GitSyncService(allowed_hosts=("github.com", "*.corp.example"))
    assert svc.validate_remote("https://github.com/o/r.git", resolve=False)
    for url, code in [("http://github.com/o/r", "invalid_remote"), ("ssh://git@github.com/o/r", "invalid_remote"), ("git@github.com:o/r.git", "invalid_remote"),
                      ("file:///etc", "invalid_remote"), ("ext::sh -c id", "invalid_remote"), ("https://user:pw@github.com/o/r", "invalid_remote"),
                      ("https://github.com/o/r?x=1", "invalid_remote"), ("https://evil.example/o/r", "host_not_allowed"),
                      ("https://github.com.evil.example/o/r", "host_not_allowed"), ("https://127.0.0.1/o/r", "invalid_remote"),
                      ("https://localhost/o/r", "invalid_remote")]:
        with pytest.raises(GitSyncError) as e:
            svc.validate_remote(url, resolve=False)
        assert e.value.code == code, url
    with pytest.raises(GitSyncError) as e:
        GitSyncService(allowed_hosts=("*",)).validate_remote("https://127.0.0.1/o/r", resolve=False)
    assert e.value.code == "invalid_remote"  # even a wildcard allow-list never reaches private addresses
    assert GitSyncService().enabled is False


@pytest.mark.parametrize("name,ok", [("main", True), ("orchivio/inv", True), ("feature-1.2", True), ("-x", False), ("a..b", False), ("a//b", False), ("x.lock", False),
                                     ("a/.b", False), ("", False), ("a b", False), ("a~1", False), ("x" * 101, False)])
def test_branch_names(name, ok):
    assert valid_branch(name) is ok


@pytest.mark.parametrize("path,ok", [("", True), ("models", True), ("a/b-c_d.e", True), ("..", False), ("a/../b", False), (".git", False), ("a/.git/x", False),
                                     ("/a", False), ("a/", False), ("a b", False)])
def test_subdir_names(path, ok):
    assert valid_subdir(path) is ok


# --- push --------------------------------------------------------------------------------------------


def test_push_commits_canonical_files_to_its_own_branch_and_keeps_other_files(world, remote, raw_model):
    a, p = world
    configure(p["admin"], a, remote, path="models/inventory")
    assert push(p["editor"], a).status_code == 403  # exporting is an admin action
    r = push(p["admin"], a)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True and body["branch"] == "orchivio/inventory" and len(body["commit"]) == 40
    tree = remote.tree("orchivio/inventory")
    assert "README.md" in tree and "models/inventory/application.yaml" in tree and any("/entities/Product.yaml" in t for t in tree)
    assert remote.tree("main") == ["README.md"]  # the base branch is untouched
    expected = model_files(raw_model)
    assert remote.read("orchivio/inventory", "models/inventory/application.yaml").strip() == expected["application.yaml"].strip()
    assert remote.log("orchivio/inventory")[0].startswith("Orchivio: inventory revision 1")
    again = push(p["admin"], a).json()
    assert again["changed"] is False and again["commit"] is None  # identical content: nothing to push
    cfg = p["admin"].get(f"/applications/{a['id']}/git-sync").json()
    assert cfg["last_push_commit"] == body["commit"] and cfg["last_push_revision_id"]


def test_push_removes_files_of_deleted_elements_and_follows_new_revisions(world, remote, raw_model):
    a, p = world
    configure(p["admin"], a, remote)
    remote.commit_files({"modules/core/entities/Stale.yaml": "x: 1\n", "notes.txt": "keep me\n"}, message="stale")
    push(p["admin"], a)
    tree = remote.tree("orchivio/inventory")
    assert "notes.txt" in tree and "README.md" in tree  # files that are not part of the format are never touched
    assert "modules/core/entities/Stale.yaml" not in tree  # but stale element files of the format are removed


def test_push_to_a_subdirectory_leaves_siblings_alone(world, remote):
    a, p = world
    remote.commit_files({"keep.txt": "1\n"}, subdir="docs")
    configure(p["admin"], a, remote, path="models")
    push(p["admin"], a)
    tree = remote.tree("orchivio/inventory")
    assert "docs/keep.txt" in tree and "models/application.yaml" in tree


def test_pushing_an_empty_repository_creates_a_root_commit(world, tmp_path):
    a, p = world
    bare = tmp_path / "empty.git"
    git("init", "--bare", "-q", "-b", "main", str(bare), cwd=tmp_path)
    assert configure(p["admin"], a, type("R", (), {"url": bare.as_uri()})).status_code == 200
    r = push(p["admin"], a)
    assert r.status_code == 200 and r.json()["changed"] is True
    assert "application.yaml" in git("ls-tree", "-r", "--name-only", "orchivio/inventory", cwd=bare)


def test_an_unreachable_remote_gives_a_fixed_message(world, tmp_path):
    a, p = world
    configure(p["admin"], a, type("R", (), {"url": (tmp_path / "nope.git").as_uri()}))
    r = push(p["admin"], a)
    assert r.status_code == 502 and r.json()["error"]["message"] in ("the repository was not found", "git ls-remote failed", "the repository could not be reached")


def test_the_token_is_resolved_at_call_time_and_never_returned(world, app, monkeypatch, remote):
    a, p = world
    seen = {}

    class Stub(GitSyncService):
        def push(self, url, token, **kw):
            seen["token"] = token
            return PushResult(False, None, None, kw["push_branch"])

    app.state.git_sync = Stub(allow_local=True)
    configure(p["admin"], a, remote, token_ref="env:ORCHIVIO_TEST_GIT_TOKEN")
    r = push(p["admin"], a)
    assert r.status_code == 409 and r.json()["error"]["code"] == "token_unavailable"
    monkeypatch.setenv("ORCHIVIO_TEST_GIT_TOKEN", "tok-s3cret-value")
    r = push(p["admin"], a)
    assert r.status_code == 200 and seen["token"] == "tok-s3cret-value"
    assert "tok-s3cret-value" not in r.text and "tok-s3cret-value" not in str(p["admin"].get(f"/applications/{a['id']}/git-sync").json())


def test_git_runs_with_a_locked_down_environment():
    svc = GitSyncService(allowed_hosts=("github.com",))
    env = svc._env("/tmp/h", "tok-123")
    assert env["GIT_ALLOW_PROTOCOL"] == "https" and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["http.followRedirects"] == "false" and pairs["core.hooksPath"] == os.devnull and pairs["core.symlinks"] == "false"
    assert "tok-123" not in " ".join(v for k, v in env.items() if not k.startswith("GIT_CONFIG_VALUE_"))  # only inside the one header value
    assert "Authorization: Basic" in pairs["http.extraHeader"]
    assert "http.extraHeader" not in {env[f"GIT_CONFIG_KEY_{i}"]: 1 for i in range(int(GitSyncService()._env("/h", None)["GIT_CONFIG_COUNT"]))}


def test_error_text_from_the_remote_is_never_returned(tmp_path):
    svc = GitSyncService(allow_local=True, git="git")
    bad = tmp_path / "x"
    with pytest.raises(GitSyncError) as e:
        svc.pull(bad.as_uri(), "tok-xyz", branch="main", subdir="")
    assert str(bad) not in e.value.message and "tok-xyz" not in e.value.message


def test_concurrent_syncs_of_one_application_are_refused(world, remote):
    from orchivio.api.routes import git_sync

    a, p = world
    configure(p["admin"], a, remote)
    import uuid

    lock = git_sync._Busy(uuid.UUID(a["id"]))
    with lock:
        r = push(p["admin"], a)
    assert r.status_code == 409 and r.json()["error"]["code"] == "sync_in_progress"


# --- pull ----------------------------------------------------------------------------------------------


def test_pull_turns_the_base_branch_into_a_proposal(world, remote, raw_model):
    a, p = world
    configure(p["admin"], a, remote)
    changed = model_files(raw_model, lambda m: m["modules"][0]["entities"][0].update(display_name="Renamed in Git"))
    remote.commit_files(changed, message="edit model")
    assert pull(p["viewer"], a).status_code == 403
    r = pull(p["editor"], a)
    assert r.status_code == 201, r.text
    prop = r.json()
    assert prop["status"] == "pending" and prop["title"].startswith("Git sync: main@") and prop["source"] == "human"
    assert p["editor"].get(f"/applications/{a['id']}").json()["current_revision_no"] == 1  # nothing applied
    assert p["admin"].get(f"/applications/{a['id']}/git-sync").json()["last_pull_commit"]


def test_pull_with_nothing_new_and_with_bad_files(world, remote, raw_model):
    a, p = world
    configure(p["admin"], a, remote)
    remote.commit_files(model_files(raw_model), message="same model")
    r = pull(p["editor"], a)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_changes"
    remote.commit_files({"surprise.txt": "x\n", "docs/readme.md": "y\n"}, message="other files are ignored")
    assert pull(p["editor"], a).json()["error"]["code"] == "no_changes"
    remote.commit_files({"modules/core/entities/Orphan.yaml": "name: Orphan\n"}, message="canonical-shaped but unreferenced")
    r = pull(p["editor"], a)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_repository_files"


def test_pull_rejects_models_that_do_not_validate(world, remote, raw_model):
    a, p = world
    configure(p["admin"], a, remote)

    def break_it(m):
        m["security"]["access"] = []

    remote.commit_files(model_files(raw_model, break_it), message="broken")
    r = pull(p["editor"], a)
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_invalid"


def test_pull_refuses_symlinks_and_other_non_regular_entries(world, remote, raw_model, tmp_path):
    a, p = world
    configure(p["admin"], a, remote)
    files = model_files(raw_model, lambda m: m["modules"][0]["entities"][0].update(display_name="x"))
    remote.commit_files(files, message="model")

    def add_symlink(clone):
        blob = git("hash-object", "-w", "--stdin", cwd=clone, input=b"/etc/passwd")
        git("update-index", "--add", "--cacheinfo", f"120000,{blob},modules/core/pages/evil.yaml", cwd=clone)

    clone_name = "link"
    (tmp_path / clone_name).mkdir(exist_ok=True)
    remote.commit_files({}, message="symlink", extra=add_symlink)
    r = pull(p["editor"], a)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsafe_entry"


def test_pull_reports_missing_branch_and_path(world, remote):
    a, p = world
    configure(p["admin"], a, remote, base_branch="release")
    assert pull(p["editor"], a).json()["error"]["code"] == "branch_not_found"
    configure(p["admin"], a, remote, path="models/missing")
    assert pull(p["editor"], a).json()["error"]["code"] == "path_not_found"


def test_the_round_trip_push_merge_pull_is_lossless(world, remote, raw_model):
    a, p = world
    configure(p["admin"], a, remote)
    push(p["admin"], a)
    # a reviewer merges the push branch into main
    git("update-ref", "refs/heads/main", "refs/heads/orchivio/inventory", cwd=remote.path)
    r = pull(p["editor"], a)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_changes"  # exactly the same model comes back


# --- pull requests -------------------------------------------------------------------------------------


class FakeTransport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, headers, body=None, *, timeout=None, max_bytes=0):
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0)


def R(status, data=None):
    import json

    return Response(status, json.dumps(data).encode() if data is not None else b"", "application/json", {})


def test_github_pull_request_is_created_or_reused():
    t = FakeTransport(R(201, {"html_url": "https://github.com/o/r/pull/7"}))
    url = GitHubPullRequests(t).open("https://github.com/o/r.git", "tok", head="orchivio/x", base="main", title="T", body="B")
    assert url == "https://github.com/o/r/pull/7"
    method, called, headers, body = t.calls[0]
    assert method == "POST" and called == "https://api.github.com/repos/o/r/pulls" and headers["Authorization"] == "Bearer tok"
    assert b'"head": "orchivio/x"' in body and b'"maintainer_can_modify": false' in body
    t = FakeTransport(R(422, {"message": "exists"}), R(200, [{"html_url": "https://github.com/o/r/pull/3"}]))
    assert GitHubPullRequests(t).open("https://github.com/o/r", "tok", head="h", base="main", title="T", body="B") == "https://github.com/o/r/pull/3"
    assert "head=o:h" in t.calls[1][1]


def test_github_pull_request_failures_are_explained_without_leaking():
    for status, expect in [(401, "refused the token"), (403, "refused the token"), (500, "HTTP 500")]:
        with pytest.raises(GitSyncError) as e:
            GitHubPullRequests(FakeTransport(R(status, {"message": "secret detail tok"}))).open("https://github.com/o/r", "tok", head="h", base="main", title="T", body="B")
        assert expect in e.value.message and "secret detail" not in e.value.message
    with pytest.raises(GitSyncError):
        GitHubPullRequests(FakeTransport()).open("https://gitlab.com/o/r", "tok", head="h", base="main", title="T", body="B")  # wrong host for this API
    with pytest.raises(GitSyncError):
        GitHubPullRequests(FakeTransport()).open("https://github.com/o", "tok", head="h", base="main", title="T", body="B")
    t = FakeTransport(R(201, {"html_url": "https://ghe.example/o/r/pull/1"}))
    assert GitHubPullRequests(t, "https://ghe.example/api/v3").open("https://ghe.example/o/r", "tok", head="h", base="main", title="T", body="B")
    assert t.calls[0][1].startswith("https://ghe.example/api/v3/repos/o/r/pulls")


def test_push_can_open_a_pull_request(world, app, monkeypatch, remote):
    a, p = world
    monkeypatch.setenv("ORCHIVIO_TEST_GIT_TOKEN", "tok")

    class Stub(GitSyncService):
        def push(self, url, token, **kw):
            return PushResult(True, "a" * 40, None, kw["push_branch"])

        def validate_remote(self, url, *, resolve=True):
            return url

    app.state.git_sync = Stub(allow_local=True)
    app.state.mcp_transport = FakeTransport(R(201, {"html_url": "https://github.com/o/r/pull/9"}))
    assert p["admin"].put(f"/applications/{a['id']}/git-sync", json={"remote_url": "https://github.com/o/r", "provider": "git", "token_ref": "env:ORCHIVIO_TEST_GIT_TOKEN"}).status_code == 200
    assert push(p["admin"], a, open_pull_request=True).json()["error"]["code"] == "pull_request_unsupported"
    p["admin"].put(f"/applications/{a['id']}/git-sync", json={"remote_url": "https://github.com/o/r", "provider": "github", "token_ref": "env:ORCHIVIO_TEST_GIT_TOKEN"})
    r = push(p["admin"], a, open_pull_request=True)
    assert r.status_code == 200 and r.json()["pull_request"] == "https://github.com/o/r/pull/9" and r.json()["pull_request_error"] is None
    app.state.mcp_transport = FakeTransport(R(500))
    r = push(p["admin"], a, open_pull_request=True)
    assert r.status_code == 200 and r.json()["pull_request"] is None and "HTTP 500" in r.json()["pull_request_error"]  # the push itself still succeeded
    p["admin"].put(f"/applications/{a['id']}/git-sync", json={"remote_url": "https://github.com/o/r", "provider": "github"})
    assert push(p["admin"], a, open_pull_request=True).json()["error"]["code"] == "token_required"


def test_removing_the_configuration(world, remote):
    a, p = world
    configure(p["admin"], a, remote)
    assert p["admin"].delete(f"/applications/{a['id']}/git-sync").status_code == 204
    assert p["admin"].get(f"/applications/{a['id']}/git-sync").status_code == 404
    assert push(p["admin"], a).status_code == 404

