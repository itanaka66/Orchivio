"""Immutable, reproducible, signed deployment packages (FR-095).

A package is a zip with fixed entry order and timestamps and no compression, so identical inputs give
byte-identical output::

    PACKAGE.json         manifest: runtime version, model hash, SHA-256 of every other file, signing key id
    PACKAGE.sig          base64 Ed25519 signature over the exact bytes of PACKAGE.json
    model.zip            the canonical model archive (docs/MODEL-SPEC.md §9)
    migration-plan.json  expand/contract steps from the previously deployed model
    sbom.json            CycloneDX-style component list of the runtime's dependencies

The signature covers the manifest and the manifest covers every file, so any change to any byte is detected.
Verification needs only the public key.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
import zlib
from dataclasses import dataclass
from importlib import metadata
from typing import Any

from orchivio.model import AppModel, CanonicalError, build_archive, model_hash, read_archive

FORMAT = "orchivio-package/1"
FILES = ("PACKAGE.json", "PACKAGE.sig", "migration-plan.json", "model.zip", "sbom.json")
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
_EPOCH = (1980, 1, 1, 0, 0, 0)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class PackageError(Exception):
    pass


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as e:  # pragma: no cover
        raise PackageError("signing needs the 'cryptography' package (pip install orchivio[delivery])") from e
    return ed25519


def generate_key() -> tuple[str, str]:
    """A new signing key as (private seed, public key), both base64 of the raw 32 bytes."""
    from cryptography.hazmat.primitives import serialization

    key = _ed25519().Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    seed = key.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    return base64.b64encode(seed).decode(), base64.b64encode(pub).decode()


def _load_private(seed_b64: str):
    try:
        seed = base64.b64decode(seed_b64, validate=True)
        return _ed25519().Ed25519PrivateKey.from_private_bytes(seed)
    except (ValueError, TypeError) as e:
        raise PackageError("the signing key must be the base64 of a raw 32-byte Ed25519 seed") from e


def _load_public(pub_b64: str):
    try:
        return _ed25519().Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64, validate=True))
    except (ValueError, TypeError) as e:
        raise PackageError("the public key must be the base64 of a raw 32-byte Ed25519 key") from e


def public_key_of(seed_b64: str) -> str:
    from cryptography.hazmat.primitives import serialization

    pub = _load_private(seed_b64).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode()


def key_id(pub_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(pub_b64)).hexdigest()[:16]


# --- migration plan -------------------------------------------------------------------------


def _entities(model: AppModel | None) -> dict[str, tuple[str, dict[str, Any]]]:
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    if model is None:
        return out
    for mod in model.modules:
        for ent in mod.entities:
            if ent.transient:
                continue
            out[ent.element_id or f"{mod.key}.{ent.name}"] = (f"{mod.key}.{ent.name}",
                                                              {f.element_id or f.name: f for f in ent.fields})
    return out


def plan_migration(old: AppModel | None, new: AppModel) -> list[dict[str, Any]]:
    """Expand/contract steps to take the data of ``old`` to ``new`` (both identified models).

    Everything additive is in the ``expand`` phase and can run before the new version is live. Anything that
    removes or narrows data is in the ``contract`` phase and is marked destructive: it runs only after the
    new version is serving traffic and is never reverted by rolling back the code. Elements are matched by
    stable id, so a rename is an add, a copy and a later drop, never a lost column.
    """
    before, after = _entities(old), _entities(new)
    steps: list[dict[str, Any]] = []

    def step(op: str, phase: str, target: str, destructive: bool = False, **extra: Any) -> None:
        steps.append({"op": op, "phase": phase, "target": target, "destructive": destructive, **extra})

    for eid, (name, fields) in after.items():
        if eid not in before:
            step("create_table", "expand", name, fields=sorted(f.name for f in fields.values()))
            continue
        old_name, old_fields = before[eid]
        if old_name != name:
            step("rename_table", "expand", name, **{"from": old_name})
        for fid, f in fields.items():
            if fid not in old_fields:
                step("add_column", "expand", f"{name}.{f.name}", type=f.type, required=f.required,
                     backfill_required=bool(f.required and f.default is None and f.default_expr is None))
                continue
            o = old_fields[fid]
            if o.name != f.name:
                step("add_column", "expand", f"{name}.{f.name}", type=f.type, required=False, copy_from=f"{name}.{o.name}")
                step("drop_column", "contract", f"{name}.{o.name}", True, reason="renamed")
            if o.type != f.type:
                step("alter_column_type", "contract", f"{name}.{f.name}", True, **{"from": o.type, "to": f.type})
            elif f.required and not o.required:
                step("set_not_null", "contract", f"{name}.{f.name}", False, check_existing_rows=True)
            elif (f.max_length or 0) and (o.max_length is None or f.max_length < o.max_length):
                step("narrow_length", "contract", f"{name}.{f.name}", True, **{"from": o.max_length, "to": f.max_length})
        for fid, o in old_fields.items():
            if fid not in fields:
                step("drop_column", "contract", f"{name}.{o.name}", True, reason="removed")
    for eid, (name, _) in before.items():
        if eid not in after:
            step("drop_table", "contract", name, True)
    order = {"expand": 0, "contract": 1}
    return sorted(steps, key=lambda s: (order[s["phase"]], s["target"], s["op"]))


# --- SBOM -----------------------------------------------------------------------------------


def sbom(distribution: str = "orchivio") -> dict[str, Any]:
    """Runtime dependencies (direct requirements without extras) with the installed versions."""
    try:
        lines = metadata.requires(distribution) or []
    except metadata.PackageNotFoundError:
        lines = []
    names = sorted({m.group(0).lower() for line in lines if "extra ==" not in line and (m := _NAME.match(line))})
    components = []
    for name in names:
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = "not-installed"
        components.append({"type": "library", "name": name, "version": version, "purl": f"pkg:pypi/{name}@{version}"})
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1, "components": components}


# --- build / verify -------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    data: bytes
    digest: str  # sha256 of the whole zip
    manifest: dict[str, Any]
    signature: str  # base64
    key_id: str


def _json(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, _EPOCH)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name])
    return out.getvalue()


def build_package(model: AppModel, *, runtime_version: str, signing_seed: str, previous: AppModel | None = None,
                  sbom_doc: dict[str, Any] | None = None) -> Package:
    pub = public_key_of(signing_seed)
    parts = {
        "model.zip": build_archive(model),
        "migration-plan.json": _json({"from": model_hash(previous) if previous else None, "to": model_hash(model),
                                      "steps": plan_migration(previous, model)}),
        "sbom.json": _json(sbom_doc if sbom_doc is not None else sbom()),
    }
    manifest = {
        "format": FORMAT,
        "runtime_version": runtime_version,
        "schema_version": model.schema_version,
        "model_hash": model_hash(model),
        "previous_model_hash": model_hash(previous) if previous else None,
        "key_id": key_id(pub),
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in sorted(parts.items())},
    }
    manifest_bytes = _json(manifest)
    signature = base64.b64encode(_load_private(signing_seed).sign(manifest_bytes)).decode()
    data = _zip({**parts, "PACKAGE.json": manifest_bytes, "PACKAGE.sig": signature.encode("ascii")})
    return Package(data, hashlib.sha256(data).hexdigest(), manifest, signature, manifest["key_id"])


@dataclass(frozen=True)
class VerifiedPackage:
    manifest: dict[str, Any]
    model: AppModel
    migration_plan: dict[str, Any]
    sbom: dict[str, Any]
    digest: str


def verify_package(data: bytes, public_keys: list[str]) -> VerifiedPackage:
    """Check structure, hashes, the signature (against any trusted key) and the model. Raises ``PackageError``."""
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError("package is too large")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise PackageError("not a valid zip file") from e
    with z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        if sorted(names) != sorted(FILES) or len(set(names)) != len(names):
            raise PackageError(f"a package contains exactly {', '.join(FILES)}")
        for i in infos:
            if i.is_dir() or i.compress_type != zipfile.ZIP_STORED or i.file_size > MAX_PACKAGE_BYTES:
                raise PackageError("entries must be plain stored files", i.filename)
        try:
            raw = {i.filename: z.read(i) for i in infos}
        except (zipfile.BadZipFile, OSError, EOFError, zlib.error) as e:
            raise PackageError(f"the zip file is damaged: {e}") from e
    try:
        manifest = json.loads(raw["PACKAGE.json"])
        recorded = manifest["files"]
    except (ValueError, KeyError, TypeError) as e:
        raise PackageError("manifest is unreadable") from e
    if manifest.get("format") != FORMAT:
        raise PackageError(f"unsupported package format {manifest.get('format')!r}")
    try:
        sig = base64.b64decode(raw["PACKAGE.sig"], validate=True)
    except ValueError as e:
        raise PackageError("signature is not valid base64") from e
    if not public_keys:
        raise PackageError("no trusted public key is configured")
    from cryptography.exceptions import InvalidSignature

    for pub in public_keys:
        if manifest.get("key_id") != key_id(pub):
            continue
        try:
            _load_public(pub).verify(sig, raw["PACKAGE.json"])
            break
        except InvalidSignature as e:
            raise PackageError("the signature does not match the manifest") from e
    else:
        raise PackageError("the package is signed with a key that is not trusted")
    for name in ("model.zip", "migration-plan.json", "sbom.json"):
        if recorded.get(name) != hashlib.sha256(raw[name]).hexdigest():
            raise PackageError("content does not match the signed manifest", name)
    try:
        model = read_archive(raw["model.zip"])
        plan, bom = json.loads(raw["migration-plan.json"]), json.loads(raw["sbom.json"])
    except (CanonicalError, ValueError) as e:
        raise PackageError(f"the model cannot be read: {e}") from e
    if model_hash(model) != manifest.get("model_hash"):
        raise PackageError("the model does not match the manifest's model hash")
    return VerifiedPackage(manifest, model, plan, bom, hashlib.sha256(data).hexdigest())
