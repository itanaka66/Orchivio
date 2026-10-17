"""Canonical file format (DESIGN.md section 8.9, FR-093) and reproducible archives (FR-005).

An application model is written as one small YAML file per element, so that a change to one
element is a change to one file and Git diffs and pull requests stay readable:

    application.yaml                       format, schema version, application info, module order
    modules/<module>/module.yaml           module id and the order of its elements
    modules/<module>/enumerations/<Name>.yaml
    modules/<module>/entities/<Name>.yaml  (fields, indexes and hooks are inside)
    modules/<module>/pages/<key>.yaml
    modules/<module>/flows/<Key>.yaml      (steps, edges and tests are inside)
    modules/<module>/integrations/<key>.yaml (only when the module consumes external APIs)
    security/security.yaml                 roles, page access, anonymous flag, rule order
    security/access/<entity>__<role>.yaml  one file per access rule

Key order inside files is the schema's declaration order, lists keep their order, and the order of
elements is recorded in the parent file, so ``from_files(to_files(m)) == m`` exactly (property tests
check this) and the same model always produces byte-identical output.

Reading is strict: unknown, unreferenced or missing files are errors, never silently ignored.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError

from .schema import AccessRule, AppModel, model_hash

FORMAT = "orchivio.model/1"
MANIFEST = "MANIFEST.json"
MAX_FILES = 2000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 20_000_000
MAX_RATIO = 200  # uncompressed/compressed, a zip-bomb guard

_SAFE_PATH = re.compile(r"^(application\.yaml|security/security\.yaml|security/access/[A-Za-z0-9_.\-]+\.yaml|"
                        r"modules/[a-z][a-z0-9_]*/(module\.yaml|(enumerations|entities|pages|flows|integrations)/[A-Za-z0-9_\-]+\.yaml))$")


class CanonicalError(ValueError):
    """The files are not a valid canonical model. ``path`` names the offending file when known."""

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.message, self.path = message, path


_UNICODE_BREAKS = re.compile("[  ]")


class _Dumper(yaml.SafeDumper):
    """PyYAML writes U+0085, U+2028 and U+2029 inside single quotes, where a YAML loader folds them
    into newlines. Double quotes with escapes keep them exact."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = '"' if _UNICODE_BREAKS.search(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def _dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


def _plain(obj: Any) -> dict[str, Any]:
    # Defaults are omitted (and restored on load), but values inside free-form dicts are kept as they are,
    # including explicit nulls, so the round trip is exact.
    return json.loads(obj.model_dump_json(exclude_defaults=True))


def _rule_file(rule: AccessRule) -> str:
    return f"security/access/{rule.entity}__{rule.role}.yaml"


# --- writing -----------------------------------------------------------------------------------


def to_files(model: AppModel) -> dict[str, str]:
    """The canonical files of ``model`` (path -> YAML text), deterministically."""
    files: dict[str, str] = {}
    seen: dict[str, str] = {}

    def put(path: str, data: Any) -> None:
        low = path.lower()
        if low in seen and seen[low] != path:
            raise CanonicalError(f"names differ only by letter case ({seen[low]} and {path}); "
                                 "that cannot be stored on case-insensitive file systems", path)
        seen[low] = path
        files[path] = _dump(data)

    put("application.yaml", {
        "format": FORMAT,
        "schema_version": model.schema_version,
        "application": _plain(model.application),
        "modules": [m.key for m in model.modules],
        **({"lint_suppressions": [_plain(s) for s in model.lint_suppressions]} if model.lint_suppressions else {}),
    })
    for mod in model.modules:
        base = f"modules/{mod.key}"
        put(f"{base}/module.yaml", {
            **({"element_id": mod.element_id} if mod.element_id else {}),
            "key": mod.key,
            "enumerations": [e.name for e in mod.enumerations],
            "entities": [e.name for e in mod.entities],
            "pages": [p.key for p in mod.pages],
            "flows": [f.key for f in mod.flows],
            **({"integrations": [i.key for i in mod.integrations]} if mod.integrations else {}),
        })
        for e in mod.enumerations:
            put(f"{base}/enumerations/{e.name}.yaml", _plain(e))
        for ent in mod.entities:
            put(f"{base}/entities/{ent.name}.yaml", _plain(ent))
        for pg in mod.pages:
            put(f"{base}/pages/{pg.key}.yaml", _plain(pg))
        for fl in mod.flows:
            put(f"{base}/flows/{fl.key}.yaml", _plain(fl))
        for ig in mod.integrations or []:
            put(f"{base}/integrations/{ig.key}.yaml", _plain(ig))
    sec = model.security
    put("security/security.yaml", {"roles": list(sec.roles), "anonymous": sec.anonymous, "pages": dict(sec.pages),
                                   "access": [_rule_file(r).removeprefix("security/access/") for r in sec.access]})
    for rule in sec.access:
        put(_rule_file(rule), _plain(rule))
    return files


# --- reading -----------------------------------------------------------------------------------


def _load(files: Mapping[str, str], path: str) -> Any:
    if path not in files:
        raise CanonicalError("file is missing", path)
    try:
        return yaml.safe_load(files[path])
    except yaml.YAMLError as e:
        raise CanonicalError(f"not valid YAML ({e.__class__.__name__})", path) from e


def _expect(data: Any, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CanonicalError("expected a mapping", path)
    return data


def _names(data: dict[str, Any], key: str, path: str) -> list[str]:
    v = data.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise CanonicalError(f"'{key}' must be a list of names", path)
    return v


def from_files(files: Mapping[str, str]) -> AppModel:
    """Rebuild a model from canonical files. Raises ``CanonicalError`` on any irregularity."""
    for path in files:
        if not _SAFE_PATH.match(path):
            raise CanonicalError("not a recognised canonical file", path)
    used: set[str] = set()

    def take(path: str) -> Any:
        used.add(path)
        return _load(files, path)

    try:
        app = _expect(take("application.yaml"), "application.yaml")
        if app.get("format") != FORMAT:
            raise CanonicalError(f"unsupported format {app.get('format')!r} (expected {FORMAT})", "application.yaml")
        modules: list[dict[str, Any]] = []
        for key in _names(app, "modules", "application.yaml"):
            mp = f"modules/{key}/module.yaml"
            m = _expect(take(mp), mp)
            if m.get("key") != key:
                raise CanonicalError(f"module key {m.get('key')!r} does not match its directory", mp)
            base = f"modules/{key}"
            modules.append({
                **({"element_id": m["element_id"]} if "element_id" in m else {}),
                "key": key,
                "enumerations": [_expect(take(f"{base}/enumerations/{n}.yaml"), f"{base}/enumerations/{n}.yaml") for n in _names(m, "enumerations", mp)],
                "entities": [_expect(take(f"{base}/entities/{n}.yaml"), f"{base}/entities/{n}.yaml") for n in _names(m, "entities", mp)],
                "pages": [_expect(take(f"{base}/pages/{n}.yaml"), f"{base}/pages/{n}.yaml") for n in _names(m, "pages", mp)],
                "flows": [_expect(take(f"{base}/flows/{n}.yaml"), f"{base}/flows/{n}.yaml") for n in _names(m, "flows", mp)],
                **({"integrations": [_expect(take(f"{base}/integrations/{n}.yaml"), f"{base}/integrations/{n}.yaml")
                                     for n in _names(m, "integrations", mp)]} if m.get("integrations") else {}),
            })
        sec = _expect(take("security/security.yaml"), "security/security.yaml")
        access = [_expect(take(f"security/access/{n}"), f"security/access/{n}")
                  for n in _names(sec, "access", "security/security.yaml")]
        extra = sorted(set(files) - used)
        if extra:
            raise CanonicalError("file is not referenced by any manifest (stale or foreign)", extra[0])
        data = {
            "schema_version": app.get("schema_version"),
            "application": app.get("application"),
            "modules": modules,
            "security": {"roles": sec.get("roles", []), "access": access, "pages": sec.get("pages", {}),
                         "anonymous": sec.get("anonymous", False)},
            "lint_suppressions": app.get("lint_suppressions", []),
        }
        return AppModel.model_validate(data)
    except ValidationError as e:
        loc = ".".join(str(x) for x in e.errors()[0]["loc"]) if e.errors() else ""
        raise CanonicalError(f"does not match the model schema at {loc}: {e.errors()[0]['msg']}" if e.errors() else "schema error") from e


# --- archives ----------------------------------------------------------------------------------

_EPOCH = (1980, 1, 1, 0, 0, 0)


def build_archive(model: AppModel) -> bytes:
    """A zip of the canonical files plus a manifest with file hashes and the model hash.

    Entries are sorted, timestamps fixed and nothing is compressed (compression output varies
    between zlib builds), so the same model always yields the same bytes.
    """
    files = to_files(model)
    manifest = {
        "format": FORMAT,
        "schema_version": model.schema_version,
        "model_hash": model_hash(model),
        "files": {p: hashlib.sha256(t.encode("utf-8")).hexdigest() for p, t in sorted(files.items())},
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for path, text in sorted({**files, MANIFEST: json.dumps(manifest, indent=2, sort_keys=True) + "\n"}.items()):
            info = zipfile.ZipInfo(path, _EPOCH)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            z.writestr(info, text.encode("utf-8"))
    return out.getvalue()


def read_archive(data: bytes) -> AppModel:
    """Read an archive produced by ``build_archive``, verifying its integrity.

    Hostile input is expected: limits on file count and sizes, no paths outside the canonical set,
    only regular files, and every file hash plus the model hash must match the manifest.
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise CanonicalError("not a valid zip archive") from e
    with z:
        infos = z.infolist()
        if len(infos) > MAX_FILES:
            raise CanonicalError(f"too many files ({len(infos)} > {MAX_FILES})")
        total = 0
        for i in infos:
            if i.is_dir() or (i.external_attr >> 16) & 0o170000 not in (0, 0o100000):
                raise CanonicalError("only regular files are allowed", i.filename)
            if i.file_size > MAX_FILE_BYTES:
                raise CanonicalError(f"file larger than {MAX_FILE_BYTES} bytes", i.filename)
            if i.compress_size and i.file_size / i.compress_size > MAX_RATIO:
                raise CanonicalError("suspicious compression ratio", i.filename)
            total += i.file_size
        if total > MAX_TOTAL_BYTES:
            raise CanonicalError(f"archive expands to more than {MAX_TOTAL_BYTES} bytes")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise CanonicalError("duplicate entries")
        raw = {i.filename: z.read(i) for i in infos}
    if MANIFEST not in raw:
        raise CanonicalError("manifest is missing", MANIFEST)
    try:
        manifest = json.loads(raw.pop(MANIFEST))
        recorded = manifest["files"]
    except (ValueError, KeyError, TypeError) as e:
        raise CanonicalError("manifest is unreadable", MANIFEST) from e
    try:
        files = {p: b.decode("utf-8") for p, b in raw.items()}
    except UnicodeDecodeError as e:
        raise CanonicalError("files must be UTF-8") from e
    for p, text in files.items():
        if recorded.get(p) != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise CanonicalError("content does not match the manifest", p)
    missing = sorted(set(recorded) - set(files))
    if missing:
        raise CanonicalError("listed in the manifest but absent", missing[0])
    model = from_files(files)
    if manifest.get("model_hash") != model_hash(model):
        raise CanonicalError("the model does not match the manifest's model hash", MANIFEST)
    return model


__all__ = ["FORMAT", "MANIFEST", "CanonicalError", "build_archive", "from_files", "read_archive", "to_files"]
