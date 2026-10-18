"""Open (or find) the pull request for a pushed branch through the GitHub REST API, over the SSRF-pinned transport."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlsplit

from orchivio.integrations.ssrf import DestinationError

from . import GitSyncError, split_github


class GitHubPullRequests:
    def __init__(self, transport: Any, api_url: str = "https://api.github.com") -> None:
        self.transport, self.api = transport, api_url.rstrip("/")

    def _call(self, method: str, path: str, token: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "User-Agent": "Orchivio-GitSync/1",
                   "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            resp = self.transport.request(method, f"{self.api}{path}", headers, data, timeout=15, max_bytes=1_000_000)
        except DestinationError as e:
            raise GitSyncError(f"the GitHub API address was refused: {e}", "pull_request_failed") from e
        except OSError as e:
            raise GitSyncError("the GitHub API could not be reached", "pull_request_failed") from e
        try:
            return resp.status, (json.loads(resp.body) if resp.body else None)
        except ValueError:
            return resp.status, None

    def open(self, remote_url: str, token: str, *, head: str, base: str, title: str, body: str) -> str:
        """The URL of the pull request from ``head`` into ``base``; an already open one is reused."""
        parsed = split_github(remote_url)
        if parsed is None:
            raise GitSyncError("pull requests need an https://host/owner/repo remote", "pull_request_failed")
        host, owner, repo = parsed
        api_host = (urlsplit(self.api).hostname or "")
        expected = "github.com" if api_host == "api.github.com" else api_host
        if host != expected:
            raise GitSyncError("the remote host does not match the configured GitHub API", "pull_request_failed")
        status, data = self._call("POST", f"/repos/{owner}/{repo}/pulls", token, {"title": title[:200], "head": head, "base": base, "body": body[:60_000], "maintainer_can_modify": False})
        if status == 201 and isinstance(data, dict) and isinstance(data.get("html_url"), str):
            return data["html_url"]
        if status == 422:  # most likely "a pull request already exists"
            s2, existing = self._call("GET", f"/repos/{owner}/{repo}/pulls?state=open&head={quote(owner)}:{quote(head)}&base={quote(base)}", token)
            if s2 == 200 and isinstance(existing, list) and existing and isinstance(existing[0].get("html_url"), str):
                return existing[0]["html_url"]
        if status in (401, 403):
            raise GitSyncError("GitHub refused the token for pull requests", "pull_request_failed")
        raise GitSyncError(f"GitHub did not create the pull request (HTTP {status})", "pull_request_failed")
