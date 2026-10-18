"""Git synchronisation of an application's canonical files (FR-093, docs/GIT-SYNC.md).

Orchivio **pushes** a revision as a commit on its own branch (never the protected base branch) and can open a pull request; people
review and merge it in Git. Orchivio **pulls** the base branch, reads the canonical files strictly and turns them into a
*proposal*; nothing from Git changes a model directly.

The remote is untrusted and the URL is configuration that people with admin rights can change, so the git runner is locked down:

* only ``https`` remotes whose host matches the operator's allow-list (``ORCHIVIO_GIT_SYNC_ALLOWED_HOSTS``; empty = disabled) and that
  resolve to public addresses; no credentials in URLs; redirects are not followed;
* git runs with an empty home, no system or user configuration, hooks disabled, prompts off, and ``GIT_ALLOW_PROTOCOL=https``;
* the token is passed in the environment as a request header, never on a command line or in a URL;
* no working tree is ever checked out: files are read with plumbing (``ls-tree``/``cat-file``) and written with a private index
  (``hash-object``/``update-index``/``write-tree``/``commit-tree``), so remote content cannot create symlinks, hooks or filters;
* only regular files are accepted when pulling (no symlinks or submodules), with count and size limits, and every path must be one
  the canonical format knows.
"""

from __future__ import annotations

import base64
import fnmatch
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from orchivio import telemetry
from orchivio.integrations.ssrf import DestinationError, Policy, check_url, resolve_public
from orchivio.model.canonical import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, is_canonical_path

BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
SUBDIR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_REPO_BYTES = 200_000_000


class GitSyncError(Exception):
    """Something the caller can act on (bad configuration, missing branch, unreachable remote). Never carries remote output."""

    def __init__(self, message: str, code: str = "git_error") -> None:
        super().__init__(message)
        self.message, self.code = message, code


def valid_branch(name: str) -> bool:
    return bool(BRANCH.match(name)) and ".." not in name and "//" not in name and not name.endswith((".lock", "/", ".")) and "/." not in name


def valid_subdir(path: str) -> bool:
    return path == "" or (bool(SUBDIR.match(path)) and ".git" not in path.split("/") and ".." not in path.split("/") and len(path) <= 100)


@dataclass(frozen=True)
class PushResult:
    changed: bool
    commit: str | None
    parent: str | None
    branch: str


@dataclass(frozen=True)
class PullResult:
    commit: str
    files: dict[str, str]


class GitSyncService:
    def __init__(self, *, allowed_hosts: tuple[str, ...] = (), allow_private: bool = False, timeout: int = 60, git: str = "git",
                 allow_local: bool = False) -> None:
        self.allowed_hosts, self.allow_private, self.timeout, self.git = allowed_hosts, allow_private, timeout, git
        self.allow_local = allow_local  # tests only: also accept file:// remotes

    # -- validation ------------------------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.allowed_hosts) or self.allow_local

    def validate_remote(self, url: str, *, resolve: bool = True) -> str:
        """The URL if it is an https remote on an allowed public host; raises ``GitSyncError`` otherwise."""
        if self.allow_local and url.startswith("file://"):
            return url
        if not self.allowed_hosts:
            raise GitSyncError("Git sync is not enabled on this server (ORCHIVIO_GIT_SYNC_ALLOWED_HOSTS is empty)", "git_sync_disabled")
        if not url.startswith("https://") or "?" in url or "#" in url:
            raise GitSyncError("use a plain https:// repository URL without credentials, query or fragment", "invalid_remote")
        try:
            dest = check_url(url, Policy(allow_http=False, allow_private=self.allow_private))
        except DestinationError as e:
            raise GitSyncError(f"the repository URL is not acceptable: {e}", "invalid_remote") from e
        if not any(fnmatch.fnmatch(dest.host.lower(), p.lower()) for p in self.allowed_hosts):
            raise GitSyncError("this host is not on the server's Git sync allow-list", "host_not_allowed")
        if resolve:
            try:
                resolve_public(dest, Policy(allow_private=self.allow_private))
            except (DestinationError, OSError) as e:
                raise GitSyncError(f"the repository host cannot be used: {e}", "invalid_remote") from e
        return url

    # -- running git -----------------------------------------------------------------------------------------------
    def _env(self, home: str, token: str | None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG") if k in os.environ}
        env.update({"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "GIT_ALLOW_PROTOCOL": "file" if self.allow_local else "https",
                    "GIT_OPTIONAL_LOCKS": "0", "GIT_AUTHOR_NAME": "Orchivio", "GIT_AUTHOR_EMAIL": "orchivio@localhost",
                    "GIT_COMMITTER_NAME": "Orchivio", "GIT_COMMITTER_EMAIL": "orchivio@localhost"})
        settings = [("http.followRedirects", "false"), ("core.hooksPath", os.devnull), ("core.fsmonitor", "false"), ("core.symlinks", "false"),
                    ("protocol.version", "2"), ("gc.auto", "0"), ("fetch.fsckObjects", "true"), ("receive.fsckObjects", "true"),
                    ("transfer.fsckObjects", "true")]
        if token:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            settings.append(("http.extraHeader", f"Authorization: Basic {basic}"))
        env["GIT_CONFIG_COUNT"] = str(len(settings))
        for i, (k, v) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
        env.update(extra or {})
        return env

    def _run(self, args: list[str], *, cwd: str, env: dict[str, str], stdin: bytes | None = None, secret: str | None = None) -> bytes:
        try:
            p = subprocess.run([self.git, *args], cwd=cwd, env=env, input=stdin, capture_output=True, timeout=self.timeout, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as e:
            raise GitSyncError("git took too long and was stopped", "git_timeout") from e
        except FileNotFoundError as e:
            raise GitSyncError("git is not installed on this server", "git_missing") from e
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace")
            if secret:
                err = err.replace(secret, "***")
            raise GitSyncError(_classify(err, args[0]), "git_failed")
        return p.stdout

    def _repo(self, tmp: str) -> tuple[str, str]:
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        return home, os.path.join(tmp, "repo.git")

    def _guard_size(self, repo: str) -> None:
        total = sum(f.stat().st_size for f in Path(repo).rglob("*") if f.is_file())
        if total > MAX_REPO_BYTES:
            raise GitSyncError("the repository is too large to synchronise", "repo_too_large")

    def _branch_tip(self, url: str, branch: str, *, cwd: str, env: dict[str, str], secret: str | None) -> str | None:
        out = self._run(["ls-remote", "--heads", "--", url, f"refs/heads/{branch}"], cwd=cwd, env=env, secret=secret).decode("ascii", "replace")
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref == f"refs/heads/{branch}" and _SHA.match(sha):
                return sha
        return None

    # -- push --------------------------------------------------------------------------------------------------------
    def push(self, url: str, token: str | None, *, base_branch: str, push_branch: str, subdir: str, files: Mapping[str, str], message: str) -> PushResult:
        """Commit ``files`` under ``subdir`` on top of ``base_branch`` and force-update ``push_branch`` to it. Files elsewhere in the
        repository are kept; files under ``subdir`` that are not in ``files`` are removed."""
        self.validate_remote(url)
        if not (valid_branch(base_branch) and valid_branch(push_branch) and valid_subdir(subdir)):
            raise GitSyncError("invalid branch or path", "invalid_config")
        if push_branch == base_branch:
            raise GitSyncError("the push branch must differ from the base branch; merge through a pull request", "invalid_config")
        with telemetry.span("gitsync.push"), tempfile.TemporaryDirectory(prefix="orchivio-git-") as tmp:
            home, repo = self._repo(tmp)
            env = self._env(home, token)
            self._run(["init", "--bare", "-q", "--", repo], cwd=tmp, env=env)
            genv = {**env, "GIT_DIR": repo}
            parent = self._branch_tip(url, base_branch, cwd=tmp, env=genv, secret=token)
            if parent:
                self._run(["fetch", "--depth", "1", "--no-tags", "-q", "--", url, f"+refs/heads/{base_branch}:refs/remotes/origin/base"],
                          cwd=tmp, env=genv, secret=token)
                self._guard_size(repo)
                got = self._run(["rev-parse", "--verify", "refs/remotes/origin/base"], cwd=tmp, env=genv).decode().strip()
                if got != parent and not _SHA.match(got):
                    raise GitSyncError("unexpected fetch result", "git_failed")
                parent = got
            index = {**genv, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            if parent:
                self._run(["read-tree", parent], cwd=tmp, env=index)
            prefix = f"{subdir}/" if subdir else ""
            lines: list[str] = []
            if parent:
                listed = self._run(["ls-files", "-z"], cwd=tmp, env=index).decode("utf-8", "surrogateescape").split("\0")
                lines += [f"0 {'0' * 40}\t{p}" for p in listed if p.startswith(prefix) and is_canonical_path(p[len(prefix):])]
            for name, text in sorted(files.items()):
                if not name or name.startswith("/") or ".." in name.split("/") or "\0" in name or "\n" in name:
                    raise GitSyncError("refusing an unsafe file path", "invalid_files")
                blob = self._run(["hash-object", "-w", "--stdin"], cwd=tmp, env=index, stdin=text.encode("utf-8")).decode().strip()
                lines.append(f"100644 {blob}\t{prefix}{name}")
            self._run(["update-index", "--index-info"], cwd=tmp, env=index, stdin=("\n".join(lines) + "\n").encode("utf-8"))
            tree = self._run(["write-tree"], cwd=tmp, env=index).decode().strip()
            if parent and self._run(["rev-parse", f"{parent}^{{tree}}"], cwd=tmp, env=genv).decode().strip() == tree:
                return PushResult(False, None, parent, push_branch)  # the base branch already has exactly this model
            if self._branch_tip(url, push_branch, cwd=tmp, env=genv, secret=token):
                self._run(["fetch", "--depth", "1", "--no-tags", "-q", "--", url, f"+refs/heads/{push_branch}:refs/remotes/origin/push"],
                          cwd=tmp, env=genv, secret=token)
                if self._run(["rev-parse", "refs/remotes/origin/push^{tree}"], cwd=tmp, env=genv).decode().strip() == tree:
                    return PushResult(False, None, parent, push_branch)  # the push branch already holds this model
            args =["commit-tree", tree, *(["-p", parent] if parent else []), "-F", "-"]
            commit = self._run(args, cwd=tmp, env=genv, stdin=message.encode("utf-8")).decode().strip()
            self._run(["push", "-q", "--", url, f"+{commit}:refs/heads/{push_branch}"], cwd=tmp, env=genv, secret=token)
            return PushResult(True, commit, parent, push_branch)

    # -- pull --------------------------------------------------------------------------------------------------------
    def pull(self, url: str, token: str | None, *, branch: str, subdir: str) -> PullResult:
        """The canonical files under ``subdir`` at the tip of ``branch``. Only regular files; nothing is checked out."""
        self.validate_remote(url)
        if not (valid_branch(branch) and valid_subdir(subdir)):
            raise GitSyncError("invalid branch or path", "invalid_config")
        with telemetry.span("gitsync.pull"), tempfile.TemporaryDirectory(prefix="orchivio-git-") as tmp:
            home, repo = self._repo(tmp)
            env = self._env(home, token)
            self._run(["init", "--bare", "-q", "--", repo], cwd=tmp, env=env)
            genv = {**env, "GIT_DIR": repo}
            if self._branch_tip(url, branch, cwd=tmp, env=genv, secret=token) is None:
                raise GitSyncError(f"the branch '{branch}' does not exist in the repository", "branch_not_found")
            self._run(["fetch", "--depth", "1", "--no-tags", "-q", "--", url, f"+refs/heads/{branch}:refs/remotes/origin/pull"], cwd=tmp, env=genv, secret=token)
            self._guard_size(repo)
            commit = self._run(["rev-parse", "--verify", "refs/remotes/origin/pull"], cwd=tmp, env=genv).decode().strip()
            if not _SHA.match(commit):
                raise GitSyncError("unexpected fetch result", "git_failed")
            treeish = f"{commit}:{subdir}" if subdir else commit
            try:
                listing = self._run(["ls-tree", "-r", "-z", "--long", treeish], cwd=tmp, env=genv)
            except GitSyncError as e:
                raise GitSyncError(f"the path '{subdir}' does not exist on branch '{branch}'", "path_not_found") from e
            entries: list[tuple[str, str]] = []
            total = 0
            for raw in listing.split(b"\0"):
                if not raw:
                    continue
                meta, _, name = raw.partition(b"\t")
                mode, kind, sha, size = meta.decode("ascii").split(None, 3)
                if not is_canonical_path(name.decode("utf-8", "replace")):
                    continue  # README, docs, CI files: not ours, never read
                if kind != "blob" or mode not in ("100644", "100755"):
                    raise GitSyncError("the repository contains symlinks, submodules or other non-regular entries under the model path", "unsafe_entry")
                total += int(size)
                if int(size) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(entries) >= MAX_FILES:
                    raise GitSyncError("the model files are larger than the import limits", "too_large")
                entries.append((name.decode("utf-8", "strict"), sha))
            files: dict[str, str] = {}
            for name, sha in entries:
                blob = self._run(["cat-file", "blob", sha], cwd=tmp, env=genv)
                try:
                    files[name] = blob.decode("utf-8")
                except UnicodeDecodeError as e:
                    raise GitSyncError(f"{name} is not valid UTF-8", "invalid_files") from e
            return PullResult(commit, files)


def _classify(stderr: str, command: str) -> str:
    """A fixed, safe sentence per failure kind; the raw text can contain remote-controlled content."""
    low = stderr.lower()
    if "authentication failed" in low or "403" in low or "401" in low or "could not read username" in low:
        return "the repository refused the credentials"
    if "not found" in low or "404" in low or "does not appear to be a git repository" in low:
        return "the repository was not found"
    if "protected branch" in low or "denied" in low or "permission" in low:
        return "the repository refused the push (permissions or branch protection)"
    if "could not resolve host" in low or "unable to access" in low or "connection" in low:
        return "the repository could not be reached"
    return f"git {command} failed"


def split_github(url: str) -> tuple[str, str, str] | None:
    """(host, owner, repo) of an https GitHub-style remote, else None."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if parts.scheme != "https" or len(segs) != 2:
        return None
    repo = segs[1][:-4] if segs[1].endswith(".git") else segs[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", segs[0]) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        return None
    return (parts.hostname or "", segs[0], repo)
