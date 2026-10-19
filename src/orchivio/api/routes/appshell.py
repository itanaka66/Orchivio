"""The installable shell of a generated application (FR-129, docs/PWA.md).

``GET /api/v1/apps/{id}/shell/`` serves a small web app (no build step, no inline script) that signs the end user in and draws the pages the page API
allows them to open. It is a Progressive Web App of its own: a manifest with generated icons, and a service worker whose scope is the shell directory only.
The application's data and sign-in live beside it (``../pages``, ``../data``, ``../auth``), outside that scope, so the worker never sees them and nothing
of the application is cached on the device except the shell itself. Offline, the shell opens and says so; data needs a connection.

Only applications that have a model have a shell; any other id gets the same 404 as everywhere else.
"""

from __future__ import annotations

import colorsys
import hashlib
import html
import json
import struct
import uuid
import zlib
from functools import lru_cache
from importlib import resources

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..deps import get_db, not_found
from .appauth import _app

router = APIRouter(tags=["application shell"], prefix="/apps/{app_id}/shell", include_in_schema=False)

_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
_NO_CACHE = {"Cache-Control": "no-cache"}
_ASSET = {"Cache-Control": "public, max-age=31536000, immutable"}


@lru_cache(maxsize=None)
def asset(name: str) -> str:
    return (resources.files("orchivio.api") / "appshell_assets" / name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def build_id() -> str:
    """Changes whenever a file of the shell changes, so installed copies notice the new release."""
    h = hashlib.sha256()
    for name in ("index.html", "app.js", "app.css", "sw.js", "offline.html"):
        h.update(asset(name).encode("utf-8"))
    return h.hexdigest()[:12]


def theme(app_id: uuid.UUID) -> tuple[int, int, int]:
    """A colour per application (white text on it stays readable), so installed applications can be told apart."""
    hue = int(hashlib.sha256(app_id.bytes).hexdigest()[:4], 16) / 0xFFFF
    r, g, b = colorsys.hls_to_rgb(hue, 0.36, 0.55)
    return round(r * 255), round(g * 255), round(b * 255)


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % c


def png(size: int, bg: tuple[int, int, int]) -> bytes:
    """A square icon: the application's colour with a lighter rounded block in the middle, inside the maskable safe zone."""
    fg = tuple(round(v + (255 - v) * 0.82) for v in bg)
    lo, hi, radius = round(size * 0.3), round(size * 0.7), round(size * 0.08)

    def inside(x: int, y: int) -> bool:
        if not (lo <= x < hi and lo <= y < hi):
            return False
        dx = max(lo + radius - x, 0, x - (hi - 1 - radius))
        dy = max(lo + radius - y, 0, y - (hi - 1 - radius))
        return dx * dx + dy * dy <= radius * radius

    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(fg if inside(x, y) else bg)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def _respond(body: str | bytes, media: str, headers: dict[str, str]) -> Response:
    return Response(body, media_type=media, headers={"Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", **headers})


def _name(app_id: uuid.UUID, db: Session) -> str:
    app, _, _, _ = _app(db, app_id)  # 404 for an unknown application and for one without a model
    return app.name


@router.get("/")
def index(app_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    name = _name(app_id, db)
    page = asset("index.html").replace("{{TITLE}}", html.escape(name)).replace("{{THEME}}", _hex(theme(app_id))).replace("{{BUILD}}", build_id())
    return _respond(page, "text/html; charset=utf-8", _NO_CACHE)


@router.get("/manifest.webmanifest")
def manifest(app_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    name = _name(app_id, db)
    colour = _hex(theme(app_id))
    doc = {"id": "./", "name": name, "short_name": name[:12], "description": f"{name} (built with Orchivio)", "start_url": "./", "scope": "./", "display": "standalone",
           "background_color": "#f6f7f9", "theme_color": colour,
           "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                     {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
    return _respond(json.dumps(doc), "application/manifest+json", _NO_CACHE)


@router.get("/sw.js")
def worker(app_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    _name(app_id, db)
    return _respond(asset("sw.js").replace("__BUILD__", build_id()), "text/javascript; charset=utf-8", _NO_CACHE)


@router.get("/app.js")
def script(app_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    _name(app_id, db)
    return _respond(asset("app.js"), "text/javascript; charset=utf-8", _ASSET)


@router.get("/app.css")
def styles(app_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    _name(app_id, db)
    return _respond(asset("app.css"), "text/css; charset=utf-8", _ASSET)


@router.get("/offline.html")
def offline(app_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    _name(app_id, db)
    return _respond(asset("offline.html"), "text/html; charset=utf-8", _NO_CACHE)


@router.get("/icon-{size}.png")
def icon(app_id: uuid.UUID, size: int, db: Session = Depends(get_db)) -> Response:
    _name(app_id, db)
    if size not in (192, 512):
        raise not_found("icon")
    return _respond(png(size, theme(app_id)), "image/png", {"Cache-Control": "public, max-age=86400"})
