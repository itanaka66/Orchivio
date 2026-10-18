"""``orchivio`` command line: work with models as files, e.g. in a Git repository and in CI.

    orchivio validate model.yaml | model-dir/ | model.zip     # exit 1 if there are errors
    orchivio lint     model.yaml
    orchivio export   model.yaml out-dir/ | out.zip [--assign-ids]
    orchivio import   model-dir/ | model.zip out.yaml
    orchivio diff     before after                            # exit 1 if they differ
    orchivio openapi  model.yaml [-o openapi.json]            # data API description
    orchivio keygen | verify-package FILE --public-key K        # signed deployment packages (docs/DELIVERY.md)
    orchivio worker [--once] [--interval S]                   # scheduled jobs and the task queue
    orchivio backup | restore | verify-backup FILE            # see docs/OPERATIONS.md

A model is a single YAML/JSON file, a directory of canonical files (docs/MODEL-SPEC.md) or a zip.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TextIO

import yaml
from pydantic import ValidationError

from .codegen import CLIENT_LANGUAGES, build_openapi
from .model import (
    AppModel,
    CanonicalError,
    assign_element_ids,
    build_archive,
    diff_models,
    from_files,
    has_errors,
    lint,
    migrate_to_latest,
    read_archive,
    to_files,
    validate_model,
)
from .model.canonical import _SAFE_PATH
from .ops import BackupError, backup, read_backup, restore

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE = 0, 1, 2


class CliError(Exception):
    pass


def load(path: str) -> AppModel:
    p = Path(path)
    try:
        if p.is_dir():
            files = {f.relative_to(p).as_posix(): f.read_text(encoding="utf-8") for f in sorted(p.rglob("*.yaml")) if f.is_file()}
            return from_files(files)
        if p.suffix == ".zip":
            return read_archive(p.read_bytes())
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise CliError(f"{path}: expected a mapping at the top level")
        return AppModel.model_validate(migrate_to_latest(raw))
    except FileNotFoundError as e:
        raise CliError(f"{path}: no such file or directory") from e
    except (CanonicalError, yaml.YAMLError, ValueError) as e:
        if isinstance(e, ValidationError):
            first = e.errors()[0]
            raise CliError(f"{path}: invalid model at {'.'.join(map(str, first['loc']))}: {first['msg']}") from e
        raise CliError(f"{path}: {e}") from e


def _write_dir(files: dict[str, str], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    for stale in out.rglob("*.yaml"):  # remove canonical files that no longer exist, keep anything else
        rel = stale.relative_to(out).as_posix()
        if rel not in files and _SAFE_PATH.match(rel):
            stale.unlink()
    for rel, text in files.items():
        target = (out / rel).resolve()
        if not target.is_relative_to(root):
            raise CliError(f"refusing to write outside {out}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def _issues(model: AppModel, out: TextIO) -> bool:
    problems = validate_model(model)
    for i in problems:
        print(f"{i.severity}: {i.path}: {i.message} [{i.code}]", file=out)
    return has_errors(problems)


def run(args: argparse.Namespace, out: TextIO) -> int:
    if args.command == "validate":
        model = load(args.path)
        failed = _issues(model, out)
        if not failed:
            print("ok: the model is valid", file=out)
        return EXIT_PROBLEMS if failed else EXIT_OK
    if args.command == "lint":
        model = assign_element_ids(load(args.path))
        if _issues(model, out):
            return EXIT_PROBLEMS
        blocking = 0
        for f in lint(model):
            if f.suppressed:
                continue
            blocking += f.severity == "error"
            print(f"{f.severity}: {f.path}: {f.message} [{f.rule_id}]", file=out)
        return EXIT_PROBLEMS if blocking else EXIT_OK
    if args.command == "export":
        model = load(args.path)
        if args.assign_ids:
            model = assign_element_ids(model)
        if args.out.endswith(".zip"):
            Path(args.out).write_bytes(build_archive(model))
        else:
            _write_dir(to_files(model), Path(args.out))
        print(f"exported to {args.out}", file=out)
        return EXIT_OK
    if args.command == "import":
        model = load(args.path)
        Path(args.out).write_text(
            yaml.safe_dump(json.loads(model.model_dump_json(exclude_none=True)), sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        print(f"wrote {args.out}", file=out)
        return EXIT_OK
    if args.command in ("backup", "restore", "verify-backup"):
        return run_ops(args, out)
    if args.command == "worker":
        return run_worker(args, out)
    if args.command == "keygen":
        from .delivery import generate_key

        seed, pub = generate_key()
        print(f"ORCHIVIO_PACKAGE_SIGNING_KEY={seed}", file=out)
        print(f"public key (for verify-package and ORCHIVIO_PACKAGE_TRUSTED_PUBLIC_KEYS): {pub}", file=out)
        print("keep the signing key secret; only the public key is needed to verify", file=sys.stderr)
        return EXIT_OK
    if args.command == "verify-package":
        from .delivery import PackageError, verify_package

        try:
            v = verify_package(Path(args.file).read_bytes(), [args.public_key])
        except PackageError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_PROBLEMS
        steps = v.migration_plan.get("steps", [])
        print(f"ok: signed by key {v.manifest['key_id']}, runtime {v.manifest['runtime_version']}, model {v.manifest['model_hash']}, "
              f"{len(steps)} migration step(s), {sum(1 for s in steps if s['destructive'])} destructive", file=out)
        return EXIT_OK
    if args.command == "openapi":
        spec = json.dumps(build_openapi(load(args.path)), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.out:
            Path(args.out).write_text(spec, encoding="utf-8", newline="\n")
            print(f"wrote {args.out}", file=out)
        else:
            out.write(spec)
        return EXIT_OK
    if args.command == "client":
        _ext, generate, _media = CLIENT_LANGUAGES[args.lang]
        model = load(args.path)
        text = generate(model, namespace=args.namespace) if args.lang == "csharp" else generate(model)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {args.out}", file=out)
        else:
            out.write(text)
        return EXIT_OK
    if args.command == "diff":
        a, b = assign_element_ids(load(args.before)), None
        b = assign_element_ids(load(args.after), previous=a)
        d = diff_models(a, b)
        for c in d.changes:
            risk = f" [{', '.join(c.risks)}]" if c.risks else ""
            print(f"{c.kind} {c.element_type} {c.name}{risk} ({c.level.name})", file=out)
        print("no differences" if d.is_empty else f"highest approval level: {d.level.name}", file=out)
        return EXIT_OK if d.is_empty else EXIT_PROBLEMS
    raise CliError(f"unknown command {args.command}")


def run_worker(args: argparse.Namespace, out: TextIO) -> int:
    """Plan schedules and run queued jobs; any number of these can run side by side."""
    import threading

    from .api.audit_sink import DbAuditSink
    from .api.settings import get_settings
    from .db import make_engine, make_session_factory
    from .integrations import dispatcher_from_settings, integration_client_factory
    from .jobs import Worker
    from .process import ticker
    from .runtime import PreviewStore

    settings = get_settings()
    sessions = make_session_factory(make_engine(settings.database_url))
    preview = PreviewStore(make_engine(settings.preview_database_url))
    dispatcher = dispatcher_from_settings(settings, sessions, preview.engine)
    clients = integration_client_factory(settings, sessions)
    worker = Worker(sessions, preview, audit=DbAuditSink(sessions),
                    lease_seconds=settings.worker_lease_seconds, per_app_limit=settings.worker_per_app_limit,
                    extra_steps=(dispatcher.tick, ticker(sessions, preview, audit=DbAuditSink(sessions), integrations=clients)), integrations=clients)
    if args.once:
        t = worker.tick()
        print(f"planned {t.planned}, ran {t.claimed}: {t.succeeded} succeeded, {t.retried} to retry, {t.dead} dead", file=out)
        return EXIT_OK
    print(f"worker {worker.id} polling every {args.interval:g}s (Ctrl-C to stop)", file=out)
    stop = threading.Event()
    try:
        worker.run_forever(args.interval, stop)
    except KeyboardInterrupt:
        stop.set()
    return EXIT_OK


def run_ops(args: argparse.Namespace, out: TextIO) -> int:
    from .db import make_engine

    def engine():
        url = args.database_url or os.environ.get("ORCHIVIO_DATABASE_URL")
        if not url:
            raise CliError("give --database-url or set ORCHIVIO_DATABASE_URL")
        return make_engine(url)

    try:
        if args.command == "verify-backup":
            manifest, _ = read_backup(Path(args.file).read_bytes())
            n = sum(t["rows"] for t in manifest["tables"].values())
            print(f"ok: {len(manifest['tables'])} tables, {n} rows, {len(manifest['artifacts'])} artifacts, "
                  f"schema revision {manifest['schema_revision']}, created {manifest['created_at']}", file=out)
        elif args.command == "backup":
            exclude = set() if args.include_sessions else {"sessions"}
            Path(args.file).write_bytes(backup(engine(), artifacts_dir=args.artifacts, exclude=exclude))
            os.chmod(args.file, 0o600)  # it contains password and token hashes
            print(f"wrote {args.file} (confidential: encrypt it and restrict access)", file=out)
        else:
            report = restore(Path(args.file).read_bytes(), engine(), artifacts_dir=args.artifacts)
            print(f"restored {sum(report.tables.values())} rows in {len(report.tables)} tables and {report.artifacts} artifacts", file=out)
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_PROBLEMS
    except FileNotFoundError as e:
        raise CliError(f"{e.filename}: no such file or directory") from e
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchivio", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("validate", "lint"):
        sub.add_parser(name).add_argument("path")
    e = sub.add_parser("export")
    e.add_argument("path")
    e.add_argument("out")
    e.add_argument("--assign-ids", action="store_true", help="give every element a stable element_id first")
    i = sub.add_parser("import")
    i.add_argument("path")
    i.add_argument("out")
    o = sub.add_parser("openapi", help="OpenAPI 3.1 document of the application's data API")
    o.add_argument("path")
    o.add_argument("-o", "--out")
    c = sub.add_parser("client", help="typed client for the application's data API (TypeScript, Python or C#)")
    c.add_argument("path")
    c.add_argument("--lang", choices=sorted(CLIENT_LANGUAGES), default="typescript")
    c.add_argument("--namespace", default="Orchivio.Client", help="C# namespace")
    c.add_argument("-o", "--out")
    b = sub.add_parser("backup", help="logical backup of the platform database and artifacts")
    b.add_argument("file")
    b.add_argument("--database-url")
    b.add_argument("--artifacts", help="artifact directory to include")
    b.add_argument("--include-sessions", action="store_true")
    r = sub.add_parser("restore", help="restore a backup into an empty, migrated database")
    r.add_argument("file")
    r.add_argument("--database-url")
    r.add_argument("--artifacts", help="directory to write artifacts into")
    sub.add_parser("keygen", help="generate an Ed25519 key for signing deployment packages")
    vp = sub.add_parser("verify-package", help="check a deployment package's hashes and signature")
    vp.add_argument("file")
    vp.add_argument("--public-key", required=True, help="base64 public key you trust")
    w = sub.add_parser("worker", help="plan scheduled jobs and run the background queue")
    w.add_argument("--once", action="store_true", help="one pass, then exit")
    w.add_argument("--interval", type=float, default=15.0)
    sub.add_parser("verify-backup").add_argument("file")
    d = sub.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    return p


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    try:
        return run(parser().parse_args(argv), out)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
