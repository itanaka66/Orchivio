"""Execute generated cases against a throwaway runtime built from the model.

Nothing here reads production data: rows come from the model and a seed (FR-128).
A case that cannot be exercised with synthetic data is reported as *skipped* with the reason,
never silently passed.
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import Engine, select

from orchivio import telemetry
from orchivio.expr import CheckEnv, EvalContext, UserRef, check, evaluate
from orchivio.expr.ast import BOOL
from orchivio.model import AppModel
from orchivio.runtime import (
    AppData,
    AppUser,
    Conflict,
    Forbidden,
    Invalid,
    PreviewStore,
    RowNotFound,
    generate,
    make_row,
)
from orchivio.security import resolve_access

from .cases import Case, generate_cases

USERS = ("t-u1", "t-u2", "t-u3")


@dataclass
class Result:
    id: str
    kind: str
    title: str
    status: str  # passed | failed | skipped
    message: str = ""
    duration_ms: float = 0.0


@dataclass
class Report:
    model_hash: str
    seed: int
    results: list[Result] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        out = {"passed": 0, "failed": 0, "skipped": 0}
        for r in self.results:
            out[r.status] += 1
        return out

    @property
    def ok(self) -> bool:
        return self.counts["failed"] == 0

    def as_dict(self) -> dict[str, Any]:
        return {"model_hash": self.model_hash, "seed": self.seed, "ok": self.ok, "counts": self.counts,
                "coverage": self.coverage, "results": [asdict(r) for r in self.results]}


class _Skip(Exception):
    pass


class Runner:
    def __init__(self, model: AppModel, data: AppData, seed: int, model_hash: str = "") -> None:
        self.model, self.data, self.seed, self.model_hash = model, data, seed, model_hash
        self.schema = data.schema
        self.rng = random.Random(f"tests:{seed}")
        self.ids: dict[str, list[Any]] = {}
        self.counter = 0

    # -- fixtures ---------------------------------------------------------------------------

    def populate(self, rows: int) -> None:
        generate(self.schema, self.data, rows_per_entity=rows, seed=self.seed, users=USERS)
        with self.data.engine.connect() as conn:
            for q, info in self.schema.entities.items():
                self.ids[q] = [r[0] for r in conn.execute(select(info.table.c[info.pk.name]))]

    def _row_for(self, q: str, owner: str) -> dict[str, Any] | None:
        info = self.schema.entities[q]
        self.counter += 1
        return make_row(self.schema, info, 10_000 + self.counter, self.rng, (owner,), self.ids, always_fill=True)

    def _fixture(self, q: str, owner: str) -> Any:
        row = self._row_for(q, owner)
        if row is None:
            raise _Skip("cannot build a valid synthetic record (required references have no targets)")
        try:
            stored = self.data.seed(q, row)
        except (Invalid, Conflict) as e:
            raise _Skip(f"synthetic record rejected by the model: {e.message}") from e
        return stored[self.schema.entities[q].pk.name]

    def _all_rows(self, q: str) -> list[dict[str, Any]]:
        info = self.schema.entities[q]
        with self.data.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(info.table)).mappings()]

    # -- cases ------------------------------------------------------------------------------

    def run(self, case: Case) -> Result:
        t0 = time.perf_counter()
        try:
            msg = getattr(self, f"_{case.kind.replace('-', '_')}")(case)
            res = Result(case.id, case.kind, case.title, "passed", msg or "")
        except _Skip as e:
            res = Result(case.id, case.kind, case.title, "skipped", str(e))
        except AssertionError as e:
            res = Result(case.id, case.kind, case.title, "failed", str(e))
        except Exception as e:  # noqa: BLE001 - an unexpected error is a failed test, not a crash
            res = Result(case.id, case.kind, case.title, "failed", f"unexpected {type(e).__name__}: {e}")
        res.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        return res

    def _access(self, c: Case) -> str:
        user = AppUser("t-matrix", frozenset({c.role}))
        q, op = c.entity, c.op
        some_id = (self.ids.get(q) or [uuid.uuid4()])[0]
        if c.expect == "deny":
            try:
                {"read": lambda: self.data.list(q, user), "create": lambda: self.data.create(q, user, {}),
                 "update": lambda: self.data.update(q, user, some_id, {}),
                 "delete": lambda: self.data.delete(q, user, some_id)}[op]()
            except Forbidden:
                return "denied as the model says"
            raise AssertionError(f"{c.role} was NOT denied {op} on {q}")
        # allowed: the operation must not be refused for lack of permission
        owner_user = AppUser(f"t-own-{c.role}", frozenset({c.role}))
        try:
            if op == "read":
                self.data.list(q, owner_user)
            elif op == "create":
                row = self._row_for(q, owner_user.id)
                if row is None:
                    raise _Skip("cannot build a valid synthetic record")
                self.data.create(q, owner_user, row)
            elif op == "update":
                pk = self._fixture(q, owner_user.id)
                self.data.update(q, owner_user, pk, {})
            elif op == "delete":
                pk = self._fixture(q, owner_user.id)
                self.data.delete(q, owner_user, pk)
        except Forbidden as e:
            if op in ("create", "update") and "row-level" in e.message:
                raise _Skip("the row constraint needs data this generator cannot construct") from e
            raise AssertionError(f"{c.role} was refused {op} on {q}: {e.message}") from e
        except RowNotFound as e:
            raise _Skip("the row constraint hides the synthetic row from this role") from e
        except (Invalid, Conflict) as e:
            raise _Skip(f"synthetic data rejected: {e.message}") from e
        return "allowed as the model says"

    def _row_constraint(self, c: Case) -> str:
        rule = next(r for r in self.model.security.access if r.element_id == c.rule)
        info = self.schema.entities[c.entity]
        res = check(rule.row_constraint, CheckEnv(self.schema.index.types_for(info.module.key), c.entity), BOOL)
        enums = {e.name: tuple(e.values) for e in info.module.enumerations}
        rows = self._all_rows(c.entity)
        if not rows:
            raise _Skip("no synthetic rows")
        pk = info.pk.name
        notes = []
        any_partial = False
        for uid in USERS:
            user = AppUser(uid, frozenset({c.role}))
            ctx_user = UserRef(uid, {c.role})
            oracle = set()
            for row in rows:
                rec = self.data.eval_record(c.entity, row, user)
                if evaluate(res.ast, EvalContext(record=rec, current_user=ctx_user, now=self.data.clock(), enums=enums)):
                    oracle.add(str(row[pk]))
            if c.op == "read":
                got = {r[pk] for r in self.data.list(c.entity, user, limit=200)["items"]}
                assert got == oracle, f"{uid}: SQL filter returned {len(got)} rows, interpreter says {len(oracle)}"
            elif c.op == "create":
                continue  # checked once below, for the whole rule
            else:
                hidden = [r for r in rows if str(r[pk]) not in oracle]
                if not hidden:
                    continue
                target = hidden[0][pk]
                try:
                    (self.data.update(c.entity, user, target, {}) if c.op == "update" else self.data.delete(c.entity, user, target))
                except RowNotFound:
                    pass
                else:
                    raise AssertionError(f"{uid} could {c.op} a row the constraint hides")
            if 0 < len(oracle) < len(rows):
                any_partial = True
        if c.op in ("create", "update"):
            checked = self._resulting_record_check(c, res, info, enums, rows)
            notes.append(f"{checked} candidate record(s) checked against the constraint")
        if not any_partial and c.op == "read":
            notes.append("constraint selected all or none of the synthetic rows for every test user")
        return "; ".join(notes)

    def _resulting_record_check(self, c: Case, res, info, enums, rows) -> int:
        """The constraint must hold for the record that results from a create or update: try
        changing each field the constraint reads and compare the runtime with the interpreter."""
        uid = USERS[0]
        user, ctx_user = AppUser(uid, frozenset({c.role})), UserRef(uid, {c.role})
        pk = info.pk.name
        editable = [f for f in info.stored_fields if f.name in res.fields and not (f.generated or f.computed or f.primary_key)]
        if not editable:
            raise _Skip("the constraint reads no editable stored field")

        def holds(rec) -> bool:
            return bool(evaluate(res.ast, EvalContext(record=rec, current_user=ctx_user, now=self.data.clock(), enums=enums)))

        checks = 0
        for f in editable:
            seen = []
            for v in [r[f.name] for r in rows if r.get(f.name) is not None]:
                if v not in seen:
                    seen.append(v)
            if f.type == "ref:system.User":
                seen += [u for u in USERS if u not in seen]
            for value in seen[:5]:
                try:
                    if c.op == "update":
                        target = self._fixture(c.entity, uid)
                        current = next(r for r in self._all_rows(c.entity) if r[pk] == target)
                        if not holds(self.data.eval_record(c.entity, current, user)):
                            continue  # this role cannot touch the fixture row; nothing to compare
                        expected = holds(self.data.eval_record(c.entity, {**current, f.name: value}, user))
                        attempt = lambda target=target, f=f, value=value: self.data.update(c.entity, user, target, {f.name: value})  # noqa: E731
                    else:
                        row = self._row_for(c.entity, uid)
                        if row is None:
                            continue
                        row[f.name] = value
                        expected = holds(self.data.candidate_record(c.entity, user, row))
                        attempt = lambda row=row: self.data.create(c.entity, user, row)  # noqa: E731
                except (_Skip, Invalid, Conflict, StopIteration):
                    continue
                try:
                    attempt()
                    allowed = True
                except Forbidden:
                    allowed = False
                except (Invalid, Conflict, RowNotFound):
                    continue
                assert allowed == expected, (
                    f"{c.op} with {f.name}={value!r}: runtime {'allowed' if allowed else 'refused'} it but the "
                    f"constraint says {'allowed' if expected else 'refused'}")
                checks += 1
        if checks == 0:
            raise _Skip("no candidate record could be built to compare")
        return checks

    def _crud(self, c: Case) -> str:
        roles = frozenset(self.model.security.roles)
        user = AppUser("t-crud", roles)
        q = c.entity
        row = self._row_for(q, user.id)
        if row is None:
            raise _Skip("cannot build a valid synthetic record")
        try:
            created = self.data.create(q, user, row)
        except Forbidden as e:
            raise _Skip(f"roles do not allow creating: {e.message}") from e
        except (Invalid, Conflict) as e:
            raise AssertionError(f"valid synthetic record was rejected: {e.message} {e.details}") from e
        pk = created[self.schema.entities[q].pk.name]
        assert self.data.get(q, user, pk)[self.schema.entities[q].pk.name] == pk, "created record cannot be read back"
        self.data.update(q, user, pk, {})
        try:
            self.data.delete(q, user, pk)
        except Forbidden:
            return "created, read and updated; delete not allowed for any role"
        try:
            self.data.get(q, user, pk)
        except RowNotFound:
            return ""
        raise AssertionError("record still readable after delete")

    def _flow(self, c: Case) -> str:
        from orchivio.logic import run_flow_test, tests_of

        found = next(((fq, f, i, t) for fq, f, i, t in tests_of(self.model) if fq == c.entity and str(i) == c.rule), None)
        if found is None:
            raise _Skip("the test no longer exists")
        out = run_flow_test(self.model, self.model_hash, self.data.engine, c.entity, found[3])
        if not out.passed:
            raise AssertionError("; ".join(out.messages))
        return f"{len(out.result.trace) if out.result else 0} step(s) traced"

    def _creator(self, q: str) -> AppUser:
        for role in self.model.security.roles:
            u = AppUser("t-validate", frozenset({role}))
            if resolve_access(self.model, [role], q, "create").allowed:
                return u
        raise _Skip("no role may create this entity")

    def _validation(self, c: Case) -> str:
        user, q = self._creator(c.entity), c.entity
        info = self.schema.entities[q]
        base = make_row(self.schema, info, 20_000 + self.counter, self.rng, (user.id,), self.ids, always_fill=True)
        self.counter += 1
        if base is None:
            raise _Skip("cannot build a valid synthetic record")

        def expect_invalid(bad: dict[str, Any], field_name: str | None) -> str:
            try:
                self.data.create(q, user, bad)
            except Invalid as e:
                if field_name and not any(d["field"] == field_name for d in (e.details or [])):
                    raise AssertionError(f"rejected, but not because of '{field_name}': {e.details}") from e
                return "rejected as the model says"
            except Forbidden as e:
                raise _Skip(f"row constraint blocked the attempt: {e.message}") from e
            raise AssertionError(f"invalid record was accepted ({c.rule} on {c.field})")

        f = next((x for x in info.entity.fields if x.name == c.field), None)
        if c.rule == "unknown_field":
            return expect_invalid({**base, "no_such_field_zz": 1}, "no_such_field_zz")
        assert f is not None
        if c.rule == "required":
            bad = {k: v for k, v in base.items() if k != f.name}
            return expect_invalid(bad, f.name)
        if c.rule == "max_length":
            return expect_invalid({**base, f.name: "x" * (f.max_length + 1)}, f.name)
        if c.rule == "minimum":
            return expect_invalid({**base, f.name: f.minimum - 1}, f.name)
        if c.rule == "maximum":
            return expect_invalid({**base, f.name: f.maximum + 1}, f.name)
        if c.rule == "enum":
            return expect_invalid({**base, f.name: "not_a_declared_value"}, f.name)
        if c.rule == "unique":
            try:
                self.data.create(q, user, base)
            except (Forbidden, Invalid, Conflict) as e:
                raise _Skip(f"could not create the first record: {e.message}") from e
            dup = make_row(self.schema, info, 30_000 + self.counter, self.rng, (user.id,), self.ids, always_fill=True)
            if dup is None:
                raise _Skip("cannot build a second record")
            dup[f.name] = base[f.name]
            try:
                self.data.create(q, user, dup)
            except Conflict:
                return "duplicate rejected"
            except (Invalid, Forbidden) as e:
                raise _Skip(f"second record rejected for another reason: {e.message}") from e
            raise AssertionError(f"duplicate '{f.name}' was accepted")
        raise _Skip(f"unknown validation rule {c.rule}")


def run_tests(model: AppModel, model_hash: str, engine: Engine, *, seed: int = 0, rows: int = 20) -> Report:
    """Build a temporary runtime in ``engine``, run every generated case, then drop it."""
    with telemetry.span("testgen.run", entities=len(model.entities)):
        app_id = uuid.uuid4()
        store = PreviewStore(engine)
        schema, data = store.reset(app_id, model, model_hash)
        report = Report(model_hash, seed)
        try:
            runner = Runner(model, data, seed, model_hash)
            runner.populate(max(1, min(rows, 100)))
            cases = generate_cases(model)
            report.results = [runner.run(c) for c in cases]
            report.coverage = _coverage(model, cases, report.results)
        finally:
            store.drop(app_id)
        return report


def _coverage(model: AppModel, cases: list[Case], results: list[Result]) -> dict[str, Any]:
    by_id = {c.id: c for c in cases}
    ran = [by_id[r.id] for r in results if r.status in ("passed", "failed")]
    data_cases = [c for c in ran if c.kind != "flow"]  # flow cases carry a flow key, not an entity
    entities = {c.entity for c in data_cases}
    persistent = {c.entity for c in cases if c.kind != "flow"}
    rules = {(c.entity, c.role) for c in data_cases if c.role}
    all_rules = {(r.entity, r.role) for r in model.security.access}
    constrained = [r for r in model.security.access if r.row_constraint]
    tested_constraints = {c.rule for c in ran if c.kind == "row-constraint"}
    flows = {f"{m.key}.{f.key}" for m in model.modules for f in m.flows}
    tested = {c.entity for c in ran if c.kind == "flow"}
    return {
        "flows": {"covered": len(tested & flows), "total": len(flows)},
        "entities": {"covered": len(entities), "total": len(persistent)},
        "access_rules": {"covered": len(rules & all_rules), "total": len(all_rules)},
        "row_constraints": {"covered": sum(1 for r in constrained if r.element_id in tested_constraints),
                            "total": len(constrained)},
    }

