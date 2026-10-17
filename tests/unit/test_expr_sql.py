"""The compiled SQL predicate must select exactly the rows the interpreter accepts."""

import sqlite3

import pytest

from orchivio.expr import (
    CheckEnv,
    EvalContext,
    NotCompilable,
    SqlContext,
    Type,
    UserRef,
    check,
    compile_predicate,
    evaluate,
)
from orchivio.expr.ast import INT, STRING, enum_t, ref_t

ENTITY = "core.Product"


class Schema:
    F = {
        "id": INT,
        "owner": ref_t("system.User"),
        "qty": INT,
        "label": Type("string", nullable=True),
        "name": STRING,
        "archived": Type("bool"),
        "status": enum_t("S"),
        "manager": ref_t("system.User", nullable=True),
    }

    def field_type(self, entity, field):
        return self.F.get(field) if entity == ENTITY else None

    def enum_values(self, name):
        return ("a", "b", "c") if name == "S" else None


ROWS = [
    dict(id=1, owner="u1", qty=0, label=None, name="Widget", archived=False, status="a", manager=None),
    dict(id=2, owner="u2", qty=5, label="x", name="Gadget_1", archived=False, status="b", manager="u1"),
    dict(id=3, owner="u1", qty=9, label="y", name="widget%", archived=True, status="c", manager="u2"),
    dict(id=4, owner="u3", qty=-3, label="x", name="Other", archived=False, status="a", manager=None),
]

EXPRS = [
    "record.owner == $currentUser",
    "record.owner != $currentUser",
    "owner == $currentUser || manager == $currentUser",
    "manager == null",
    "manager != null",
    "manager == $currentUser",
    "manager != $currentUser",
    "label == 'x'",
    "label != 'x'",
    "label == null || label == 'y'",
    "(label ?? 'none') == 'none'",
    "qty > 0 && !archived",
    "qty * 2 - 1 >= 9",
    "qty in [0, 9]",
    "status in [S.a, S.c]",
    "status == S.b ? true : qty > 0",
    "name.startsWith('Wid')",
    "name.contains('_')",
    "name.contains('%')",
    "name.lower() == 'widget'",
    "$currentUser.hasRole('editor') && qty >= 0",
    "$currentUser.hasRole('admin') || owner == $currentUser",
]


def _filter_interp(src, user):
    res = check(src, CheckEnv(Schema(), ENTITY))
    out = []
    for row in ROWS:
        ctx = EvalContext(record=row, current_user=user, enums={"S": ("a", "b", "c")})
        if evaluate(res.ast, ctx):
            out.append(row["id"])
    return out


def _filter_sql(src, user):
    res = check(src, CheckEnv(Schema(), ENTITY))
    pred = compile_predicate(res, SqlContext(current_user=user, dialect="sqlite"))
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE t (id INT, owner TEXT, qty INT, label TEXT, name TEXT,"
        " archived INT, status TEXT, manager TEXT)"
    )
    db.executemany(
        "INSERT INTO t VALUES (:id,:owner,:qty,:label,:name,:archived,:status,:manager)", ROWS
    )
    rows = db.execute(f"SELECT id FROM t WHERE {pred.sql} ORDER BY id", pred.params).fetchall()
    return [r[0] for r in rows]


@pytest.mark.parametrize("src", EXPRS)
@pytest.mark.parametrize("user", [UserRef("u1", {"editor"}), UserRef("u2")])
def test_sql_matches_interpreter(src, user):
    assert _filter_sql(src, user) == _filter_interp(src, user)


def test_values_are_bound_not_inlined():
    res = check("label == \"x'; DROP TABLE t; --\"", CheckEnv(Schema(), ENTITY))
    pred = compile_predicate(res)
    assert "DROP" not in pred.sql
    assert "DROP TABLE" in next(iter(pred.params.values()))


@pytest.mark.parametrize("src", ["qty / 2 > 1", "qty % 2 == 0", "size(name) > 1"])
def test_uncompilable_constructs_are_rejected(src):
    res = check(src, CheckEnv(Schema(), ENTITY))
    with pytest.raises(NotCompilable):
        compile_predicate(res)


def test_non_boolean_is_not_a_predicate():
    with pytest.raises(NotCompilable):
        compile_predicate(check("qty + 1", CheckEnv(Schema(), ENTITY)))
