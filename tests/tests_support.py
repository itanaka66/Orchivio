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
