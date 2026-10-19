"""The installable shell of a generated application (docs/PWA.md): served for applications with a model, safe, and its worker stays in its lane."""

import json
import re
import struct
import uuid
import zlib

import pytest
from test_pages import world  # noqa: F401  (fixture)

from orchivio.api.routes.appshell import build_id, png, theme
from orchivio.db.models import Application

NIL = str(uuid.uuid4())


def shell(c, path=""):
    return c.c.get(f"{c.base}/shell/{path}")


def test_the_shell_is_served_to_anyone_and_names_the_application(world):  # noqa: F811
    _, _, users, _ = world
    r = shell(users["vera"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>Inv</title>" in r.text and 'rel="manifest"' in r.text and "<script src=\"app.js?v=" in r.text
    assert "{{" not in r.text  # every placeholder was filled
    assert r.headers["cache-control"] == "no-cache"


def test_the_page_runs_no_inline_script_and_the_policy_forbids_everything_else(world):  # noqa: F811
    _, _, users, _ = world
    r = shell(users["vera"])
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text) and " onclick=" not in r.text.lower()
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_name_is_escaped(app, world):  # noqa: F811
    a, _, users, _ = world
    with app.state.session_factory() as s:
        row = s.get(Application, uuid.UUID(a["id"]))
        row.name = "<img src=x onerror=alert(1)>"
        s.commit()
    html = shell(users["vera"]).text
    assert "<img" not in html and "&lt;img" in html


def test_manifest_icons_and_worker(world):  # noqa: F811
    a, _, users, _ = world
    c = users["vera"]
    m = shell(c, "manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    doc = m.json()
    assert doc["name"] == "Inv" and doc["display"] == "standalone" and doc["scope"] == "./" and doc["start_url"] == "./"
    for icon in doc["icons"]:
        r = shell(c, icon["src"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        size = int(icon["sizes"].split("x")[0])
        assert struct.unpack(">II", r.content[16:24]) == (size, size)
    assert shell(c, "icon-64.png").status_code == 404
    sw = shell(c, "sw.js")
    assert sw.headers["content-type"].startswith("text/javascript") and build_id() in sw.text and "__BUILD__" not in sw.text
    assert sw.headers["cache-control"] == "no-cache"
    assert "immutable" in shell(c, "app.js").headers["cache-control"]
    assert shell(c, "offline.html").status_code == 200


def test_the_worker_can_only_see_the_shell_never_the_data(world):  # noqa: F811
    _, _, users, _ = world
    sw = shell(users["vera"], "sw.js").text
    # the shell lives at .../apps/<id>/shell/, the worker's scope; the data, sign-in and page API are one level up, outside it
    assert "startsWith(scope)" in sw and "request.method !== 'GET'" in sw
    assert not re.search(r"cache\.put|addAll\(\[?'?[^)]*(/pages|/data|/auth)", sw)
    assert "'../" not in sw and '"../' not in sw


def test_png_icons_are_valid_and_distinct_per_application():
    data = png(192, (36, 87, 214))
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == 192 * (192 * 3 + 1)
    assert theme(uuid.UUID(int=1)) != theme(uuid.UUID(int=2))


def test_an_unknown_application_has_no_shell(world):  # noqa: F811
    _, _, users, _ = world
    c = users["vera"].c
    for path in ("", "manifest.webmanifest", "sw.js", "app.js", "icon-192.png"):
        r = c.get(f"/api/v1/apps/{NIL}/shell/{path}")
        assert r.status_code == 404, path


def test_the_page_api_describes_the_fields_the_shell_draws(world):  # noqa: F811
    _, _, users, _ = world
    nav = users["ed"].get("/pages").json()["items"]
    create = next(p for p in nav if p["key"] == "product-create")
    names = [f["name"] for f in create["fields"]]
    assert names == ["name", "sku", "quantity", "reorder_level"]
    assert all({"label", "type", "required", "read_only"} <= set(f) for f in create["fields"])
    assert json.dumps(create)  # plain data


@pytest.mark.parametrize("name", ["app.js", "sw.js", "index.html", "app.css", "offline.html"])
def test_assets_are_packaged(name):
    from orchivio.api.routes.appshell import asset
    assert len(asset(name)) > 50
