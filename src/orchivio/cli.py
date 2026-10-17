"""``orchivio`` command line: work with models as files, e.g. in a Git repository and in CI.

    orchivio validate model.yaml | model-dir/ | model.zip     # exit 1 if there are errors
    orchivio lint     model.yaml
    orchivio export   model.yaml out-dir/ | out.zip [--assign-ids]
    orchivio import   model-dir/ | model.zip out.yaml
    orchivio diff     before after                            # exit 1 if they differ

A model is a single YAML/JSON file, a directory of canonical files (docs/MODEL-SPEC.md) or a zip.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

import yaml
from pydantic import ValidationError

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
