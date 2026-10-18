"""Helpers shared by test modules."""

from pathlib import Path

import yaml
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "inventory-app"


def load_yaml(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def alembic_config(connection) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


class Api:
    """A browser-like client: keeps the session cookie and sends the CSRF header."""

    PASSWORD = "correct horse battery"

    def __init__(self, app):
        from fastapi.testclient import TestClient

        self.c = TestClient(app, raise_server_exceptions=False)
        self.csrf: str | None = None
        self.user: dict | None = None

    def request(self, method: str, url: str, **kw):
        headers = dict(kw.pop("headers", {}))
        if self.csrf and "X-CSRF-Token" not in headers:
            headers["X-CSRF-Token"] = self.csrf
        if not url.startswith("/"):
            url = "/" + url
        if not url.startswith(("/healthz", "/api/")):
            url = "/api/v1" + url
        return self.c.request(method, url, headers=headers, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    def signup(self, email: str, name: str = "User") -> "Api":
        r = self.post("/auth/register", json={"email": email, "password": self.PASSWORD, "display_name": name})
        assert r.status_code == 201, r.text
        self.user = r.json()
        return self.login(email)

    def login(self, email: str, password: str | None = None) -> "Api":
        r = self.post("/auth/login", json={"email": email, "password": password or self.PASSWORD})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]
        return self


def with_process(raw: dict) -> dict:
    """The sample model plus an approval process that exercises every step type."""
    import copy

    m = copy.deepcopy(raw)
    m["modules"][0]["processes"] = [{
        "key": "Approval", "description": "Approve a request", "start_roles": ["viewer", "editor"], "run_as_role": "admin",
        "params": [{"name": "amount", "type": "integer"}, {"name": "note", "type": "string"}],
        "steps": [
            {"id": "start", "type": "start"},
            {"id": "review", "type": "user_task", "config": {
                "title": "Review request", "assignee_role": "editor", "outcomes": ["approve", "reject", "more_info"],
                "form": [{"name": "reviewer_comment", "type": "string", "required": False}],
                "due_in_hours": 24, "escalate_after_hours": 8, "escalate_to_role": "admin"}},
            {"id": "fix", "type": "user_task", "config": {"title": "Add information", "assignee_expr": "initiator", "outcomes": ["done"]}},
            {"id": "big", "type": "decision", "config": {"expr": "amount > 1000"}},
            {"id": "manager", "type": "user_task", "config": {"title": "Manager approval", "assignee_role": "admin", "outcomes": ["approve", "reject"]}},
            {"id": "fork", "type": "parallel_split"},
            {"id": "notify", "type": "system_task", "config": {"flow": "ReorderLowStock", "args": {"min_gap": "0"}}},
            {"id": "cool", "type": "timer", "config": {"wait_seconds": 3600}},
            {"id": "join", "type": "parallel_join"},
            {"id": "done", "type": "end"},
            {"id": "rejected", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "review"},
            {"id": "e2", "source": "review", "target": "big", "outcome": "approve"},
            {"id": "e3", "source": "review", "target": "rejected", "outcome": "reject"},
            {"id": "e4", "source": "review", "target": "fix", "outcome": "more_info"},
            {"id": "e5", "source": "fix", "target": "review", "outcome": "done"},
            {"id": "e6", "source": "big", "target": "manager", "outcome": "true"},
            {"id": "e7", "source": "big", "target": "fork", "outcome": "false"},
            {"id": "e8", "source": "manager", "target": "fork", "outcome": "approve"},
            {"id": "e9", "source": "manager", "target": "rejected", "outcome": "reject"},
            {"id": "e10", "source": "fork", "target": "notify"},
            {"id": "e11", "source": "fork", "target": "cool"},
            {"id": "e12", "source": "notify", "target": "join"},
            {"id": "e13", "source": "cool", "target": "join"},
            {"id": "e14", "source": "join", "target": "done"},
        ],
    }]
    return m
