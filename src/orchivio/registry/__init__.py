"""Module and template registry (FR-126, docs/REGISTRY.md).

A registry package is one module (or one whole starter application, a *template*) cut out of an application model, versioned with
semver and signed with the same Ed25519 keys as deployment packages. Installing never edits a model: the package is verified, merged
into the application's current model and offered as a proposal, so validation, policy packs and approval levels all apply.

Package layout (stored zip, fixed order and timestamps, so identical inputs give identical bytes)::

    REGISTRY.json   manifest: name, version, kind, description, model hash, contents, hashes of the other files, signing key id
    REGISTRY.sig    base64 Ed25519 signature over the exact bytes of REGISTRY.json
    model.zip       canonical archive (docs/MODEL-SPEC.md) of a standalone model holding only this module and the security it needs
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from orchivio.delivery.package import (
    _json,
    _load_private,
    _load_public,
    _zip,
    key_id,
    public_key_of,
)
from orchivio.model import (
    AppModel,
    CanonicalError,
    build_archive,
    model_hash,
    read_archive,
    validate_model,
)
from orchivio.model.ids import iter_elements
from orchivio.model.schema import AccessRule, ApplicationInfo, SecurityModel

from .semver import Version, VersionError, resolve, satisfies

FORMAT = "orchivio-registry/1"
FILES = ("REGISTRY.json", "REGISTRY.sig", "model.zip")
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


class RegistryError(Exception):
    def __init__(self, message: str, code: str = "registry_error", details: Any = None) -> None:
        super().__init__(message)
        self.message, self.code, self.details = message, code, details


# --- cutting a package out of a model -------------------------------------------------------------------------------


def _strings(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _strings(v)
    return out


def slice_module(model: AppModel, module_key: str, *, name: str) -> AppModel:
    """A standalone model with only ``module_key`` and the security slice it needs (access rules on its entities, page access for its
    pages, and the roles those or the module itself name). Raises ``RegistryError`` with the validation issues if it is not
    self-contained (it uses another module's entities or flows)."""
    mod = next((m for m in model.modules if m.key == module_key), None)
    if mod is None:
        raise RegistryError(f"the model has no module '{module_key}'", "unknown_module")
    rules = [r for r in model.security.access if r.entity.split(".")[0] == module_key]
    page_keys = {p.key for p in mod.pages}
    pages = {k: v for k, v in model.security.pages.items() if k in page_keys}
    body = {"module": mod.model_dump(mode="json", exclude_none=True), "rules": [r.model_dump(mode="json", exclude_none=True) for r in rules], "pages": pages}
    named = _strings(body)
    roles = [r for r in model.security.roles if r in named]
    sliced = AppModel(
        application=ApplicationInfo(key=name, name=name),
        modules=[mod.model_copy(deep=True)],
        security=SecurityModel(roles=roles, access=[r.model_copy(deep=True) for r in rules], pages={k: list(v) for k, v in pages.items()}),
    )
    errors = [i.as_dict() for i in validate_model(sliced) if i.severity == "error"]
    if errors:
        raise RegistryError("the module is not self-contained: it needs things that are not part of it", "not_self_contained", errors[:30])
    return sliced


# --- building and verifying packages ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryPackage:
    data: bytes
    digest: str
    manifest: dict[str, Any]
    signature: str
    key_id: str


def contents_of(model: AppModel, kind: str) -> dict[str, Any]:
    return {
        "modules": [m.key for m in model.modules],
        "entities": sorted(f"{m.key}.{e.name}" for m in model.modules for e in m.entities),
        "flows": sorted(f"{m.key}.{f.key}" for m in model.modules for f in m.flows),
        "pages": sorted(f"{m.key}.{p.key}" for m in model.modules for p in m.pages),
        "processes": sorted(f"{m.key}.{p.key}" for m in model.modules for p in (m.processes or [])),
        "roles": sorted(model.security.roles),
        "access_rules": len(model.security.access),
    }


def build_package(model: AppModel, *, name: str, version: str, kind: str, description: str, license: str, runtime_version: str,
                  signing_seed: str) -> RegistryPackage:
    if not NAME.match(name):
        raise RegistryError("names are lower-case letters, digits and -, 2-63 characters, starting with a letter", "invalid_name")
    try:
        Version.parse(version)
    except VersionError as e:
        raise RegistryError(str(e), "invalid_version") from e
    if kind not in ("module", "template"):
        raise RegistryError("kind is 'module' or 'template'", "invalid_kind")
    pub = public_key_of(signing_seed)
    parts = {"model.zip": build_archive(model)}
    manifest = {
        "format": FORMAT, "name": name, "version": version, "kind": kind, "description": description[:500], "license": license[:100],
        "schema_version": model.schema_version, "min_runtime_version": runtime_version, "model_hash": model_hash(model),
        "contents": contents_of(model, kind), "key_id": key_id(pub),
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in sorted(parts.items())},
    }
    manifest_bytes = _json(manifest)
    signature = base64.b64encode(_load_private(signing_seed).sign(manifest_bytes)).decode()
    data = _zip({**parts, "REGISTRY.json": manifest_bytes, "REGISTRY.sig": signature.encode("ascii")})
    return RegistryPackage(data, hashlib.sha256(data).hexdigest(), manifest, signature, manifest["key_id"])


@dataclass(frozen=True)
class VerifiedRegistryPackage:
    manifest: dict[str, Any]
    model: AppModel
    digest: str


def verify_package(data: bytes, public_keys: list[str]) -> VerifiedRegistryPackage:
    """Structure, hashes, signature (any trusted key) and model. Hostile input is expected."""
    if len(data) > MAX_PACKAGE_BYTES:
        raise RegistryError("the package is too large", "package_invalid")
    try:
        z = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as e:
        raise RegistryError("not a valid zip file", "package_invalid") from e
    with z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        if sorted(names) != sorted(FILES):
            raise RegistryError(f"a registry package contains exactly {', '.join(FILES)}", "package_invalid")
        for i in infos:
            if i.is_dir() or i.compress_type != zipfile.ZIP_STORED or i.file_size > MAX_PACKAGE_BYTES:
                raise RegistryError("entries must be plain stored files", "package_invalid")
        try:
            raw = {i.filename: z.read(i) for i in infos}
        except (zipfile.BadZipFile, OSError, EOFError, zlib.error) as e:
            raise RegistryError("the zip file is damaged", "package_invalid") from e
    try:
        manifest = json.loads(raw["REGISTRY.json"])
        recorded = manifest["files"]
        name, version = manifest["name"], manifest["version"]
    except (ValueError, KeyError, TypeError) as e:
        raise RegistryError("the manifest is unreadable", "package_invalid") from e
    if manifest.get("format") != FORMAT:
        raise RegistryError(f"unsupported package format {manifest.get('format')!r}", "package_invalid")
    if not isinstance(name, str) or not NAME.match(name):
        raise RegistryError("the manifest has an invalid name", "package_invalid")
    try:
        Version.parse(version)
        sig = base64.b64decode(raw["REGISTRY.sig"], validate=True)
    except (VersionError, ValueError) as e:
        raise RegistryError("the manifest version or the signature is invalid", "package_invalid") from e
    if not public_keys:
        raise RegistryError("no trusted public key is configured on this server", "no_trusted_key")
    from cryptography.exceptions import InvalidSignature

    for pub in public_keys:
        if manifest.get("key_id") != key_id(pub):
            continue
        try:
            _load_public(pub).verify(sig, raw["REGISTRY.json"])
            break
        except InvalidSignature as e:
            raise RegistryError("the signature does not match the manifest", "bad_signature") from e
    else:
        raise RegistryError("the package is signed with a key this server does not trust", "untrusted_key")
    if recorded.get("model.zip") != hashlib.sha256(raw["model.zip"]).hexdigest():
        raise RegistryError("the content does not match the signed manifest", "bad_signature")
    try:
        model = read_archive(raw["model.zip"])
    except CanonicalError as e:
        raise RegistryError(f"the model cannot be read: {e}", "package_invalid") from e
    if model_hash(model) != manifest.get("model_hash"):
        raise RegistryError("the model does not match the manifest's model hash", "package_invalid")
    return VerifiedRegistryPackage(manifest, model, hashlib.sha256(data).hexdigest())


# --- installing ---------------------------------------------------------------------------------------------------------


def module_hash(model: AppModel, module_key: str) -> str | None:
    mod = next((m for m in model.modules if m.key == module_key), None)
    if mod is None:
        return None
    rules = sorted((r.model_dump(mode="json", exclude_none=True) for r in model.security.access if r.entity.split(".")[0] == module_key),
                   key=lambda r: (r["entity"], r["role"]))
    blob = json.dumps({"module": _without_ids(mod.model_dump(mode="json", exclude_none=True)), "rules": [_without_ids(r) for r in rules]}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def _without_ids(obj: Any) -> Any:
    """Element ids differ between applications; a module is 'unchanged' when everything else is."""
    if isinstance(obj, dict):
        return {k: _without_ids(v) for k, v in obj.items() if k != "element_id"}
    if isinstance(obj, list):
        return [_without_ids(v) for v in obj]
    return obj


def merge_module(target: AppModel, package: AppModel, *, replace: bool) -> AppModel:
    """``target`` plus the single module of ``package`` with its roles, access rules and page access. If the module already exists it is
    replaced only when ``replace`` is set (an upgrade of an unmodified install)."""
    mod = package.modules[0]
    new = target.model_copy(deep=True)
    exists = any(m.key == mod.key for m in new.modules)
    if exists and not replace:
        raise RegistryError(f"the application already has a module '{mod.key}'", "module_exists")
    if exists:
        new.modules = [m for m in new.modules if m.key != mod.key]
        new.security.access = [r for r in new.security.access if r.entity.split(".")[0] != mod.key]
        old_pages = {p.key for m in target.modules if m.key == mod.key for p in m.pages}
        new.security.pages = {k: v for k, v in new.security.pages.items() if k not in old_pages}
    # ids of everything that stays; ids from the package that clash with them are dropped and assigned afresh
    mine = {el.element_id for _key, el in iter_elements(new) if el.element_id}
    mod_copy = mod.model_copy(deep=True)
    rules_copy = [r.model_copy(deep=True) for r in package.security.access]
    scratch = AppModel.model_construct(schema_version="1.1", application=package.application, modules=[mod_copy], security=SecurityModel.model_construct(
        roles=[], access=rules_copy, pages={}, anonymous=False), lint_suppressions=[])
    for _key, el in iter_elements(scratch):
        if el.element_id in mine:
            el.element_id = None
    new.modules.append(mod_copy)
    for r in package.security.roles:
        if r not in new.security.roles:
            new.security.roles.append(r)
    new.security.access.extend(rules_copy)
    for k, roles in package.security.pages.items():
        new.security.pages[k] = list(roles)
    return new


def template_model(package: AppModel, *, key: str, name: str) -> AppModel:
    out = package.model_copy(deep=True)
    out.application = ApplicationInfo(key=key, name=name, version=package.application.version, default_locale=package.application.default_locale,
                                      locales=list(package.application.locales))
    return out


__all__ = ["FORMAT", "NAME", "AccessRule", "RegistryError", "RegistryPackage", "Version", "VersionError", "VerifiedRegistryPackage", "build_package",
           "contents_of", "merge_module", "module_hash", "resolve", "satisfies", "slice_module", "template_model", "verify_package"]
