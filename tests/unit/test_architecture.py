"""Enforce the package dependency rules in docs/ARCHITECTURE.md (DESIGN.md section 14)."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "orchivio"

# package -> sibling packages it may import
ALLOWED: dict[str, set[str]] = {
    "ai": {"llm", "model"},
    "auth": set(),
    "ops": {"db", "ports"},
    "delivery": {"model"},
    "jobs": {"db", "logic", "model", "ports", "runtime"},
    "codegen": {"model", "security"},
    "logic": {"expr", "model", "runtime"},
    "testgen": {"expr", "logic", "model", "runtime", "security"},
    "runtime": {"expr", "model", "policy", "ports", "security"},
    "expr": set(),
    "policy": set(),
    "llm": set(),
    "ports": set(),
    "model": {"expr", "policy"},
    "security": {"expr", "model", "policy"},
    "db": {"model", "policy"},
    "api": {"delivery", "jobs", "ai", "auth", "codegen", "logic", "runtime", "testgen", "expr", "model", "security", "policy", "db", "llm", "ports"},
}


def imported_siblings(package: str) -> set[str]:
    found: set[str] = set()
    for path in (SRC / package).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                parts = name.split(".")
                if parts[0] == "orchivio" and len(parts) > 1 and parts[1] != package:
                    found.add(parts[1])
    return found


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_package_dependencies_respect_boundaries(package):
    extra = imported_siblings(package) - ALLOWED[package]
    assert not extra, f"orchivio.{package} must not import {sorted(extra)}"


def test_every_package_is_covered():
    on_disk = {p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert on_disk == set(ALLOWED)
