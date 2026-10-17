"""Semantic three-way merge of application models (FR-092, DESIGN.md section 8.8).

Models are merged element by element, using stable ``element_id`` s rather than text:

* a change made on only one side is taken;
* both sides changing *different* properties of an element merge automatically;
* both sides changing the *same* property differently, one side deleting what the other edited, a
  child added under an element the other side deleted, and divergent re-ordering are **conflicts**
  that a person resolves explicitly (``resolutions``); nothing is guessed;
* a few collections merge as sets or keyed maps so independent edits stay independent: enum values,
  roles, operations, locales, entity hooks, page access, flow step configuration, and the steps,
  edges, parameters and tests of a flow (layout positions never conflict).

The merge is deterministic and symmetric in what it flags. ``merge`` returns a model only when every
conflict has a resolution; the caller must still validate and lint the result (a merge can be
textually clean and semantically broken, for example a field removed on one branch and used by a
page on the other).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .schema import AppModel, ApplicationInfo, LintSuppression, ModuleDef, SecurityModel

ROOT = "root"
APP = "application"
SEC = "security"


@dataclass
class Node:
    id: str
    kind: str
    parent: str | None
    slot: str | None
    props: dict[str, Any]

    @property
    def label(self) -> str:
        for k in ("name", "key", "id", "rule"):
            if isinstance(self.props.get(k), str):
                return str(self.props[k])
        return self.id


@dataclass
class Flat:
    nodes: dict[str, Node] = field(default_factory=dict)
    order: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def add(self, node: Node) -> None:
        self.nodes[node.id] = node
        if node.parent is not None and node.slot is not None:
            self.order.setdefault((node.parent, node.slot), []).append(node.id)


def _dump(obj: Any, exclude: set[str]) -> dict[str, Any]:
    return json.loads(obj.model_dump_json(exclude=exclude | {"element_id"}))


def flatten(m: AppModel) -> Flat:
    """The model as a flat set of nodes plus the order of every child list."""
    f = Flat()
    f.add(Node(APP, "application", None, None, _dump(m.application, set())))
    f.add(Node(SEC, "security", None, None, {"roles": list(m.security.roles), "anonymous": m.security.anonymous,
                                              "pages": {k: list(v) for k, v in m.security.pages.items()}}))
    for mod in m.modules:
        mid = _need(mod.element_id, mod.key)
        f.add(Node(mid, "module", ROOT, "modules", {"key": mod.key}))
        for e in mod.enumerations:
            f.add(Node(_need(e.element_id, e.name), "enum", mid, "enumerations", _dump(e, set())))
        for ent in mod.entities:
            eid = _need(ent.element_id, ent.name)
            f.add(Node(eid, "entity", mid, "entities", _dump(ent, {"fields"})))
            for fld in ent.fields:
                f.add(Node(_need(fld.element_id, fld.name), "field", eid, "fields", _dump(fld, set())))
        for pg in mod.pages:
            f.add(Node(_need(pg.element_id, pg.key), "page", mid, "pages", _dump(pg, set())))
        for fl in mod.flows:
            fid = _need(fl.element_id, fl.key)
            f.add(Node(fid, "flow", mid, "flows", _dump(fl, {"steps", "edges", "params", "tests"})))
            for s in fl.steps:
                f.add(Node(f"{fid}:step:{s.id}", "step", fid, "steps", _dump(s, set())))
            for e in fl.edges:
                f.add(Node(f"{fid}:edge:{e.id}", "edge", fid, "edges", _dump(e, set())))
            for p in fl.params:
                f.add(Node(f"{fid}:param:{p.name}", "param", fid, "params", _dump(p, set())))
            for t in fl.tests:
                f.add(Node(f"{fid}:test:{t.name}", "test", fid, "tests", _dump(t, set())))
    for r in m.security.access:
        f.add(Node(_need(r.element_id, f"{r.entity}/{r.role}"), "access", SEC, "access", _dump(r, set())))
    for s in m.lint_suppressions:
        f.add(Node(f"lint:{s.rule}:{s.element_id}", "lint", ROOT, "lint", _dump(s, set())))
    return f


def _need(element_id: str | None, what: str) -> str:
    if not element_id:
        raise ValueError(f"cannot merge: '{what}' has no element_id (run assign_element_ids first)")
    return element_id


def unflatten(f: Flat, *, schema_version: str = "1.1") -> AppModel:
    """Inverse of ``flatten``. Raises ``ValueError`` if the structure is inconsistent."""

    def kids(parent: str, slot: str) -> list[Node]:
        return [f.nodes[i] for i in f.order.get((parent, slot), []) if i in f.nodes]

    def with_id(n: Node, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {**n.props, **(extra or {}), **({"element_id": n.id} if n.kind in ("module", "enum", "entity", "field", "page", "flow", "access") else {})}

    modules = []
    for mn in kids(ROOT, "modules"):
        modules.append(ModuleDef.model_validate({
            **with_id(mn),
            "enumerations": [with_id(e) for e in kids(mn.id, "enumerations")],
            "entities": [with_id(e, {"fields": [with_id(x) for x in kids(e.id, "fields")]}) for e in kids(mn.id, "entities")],
            "pages": [with_id(p) for p in kids(mn.id, "pages")],
            "flows": [with_id(fl, {"steps": [x.props for x in kids(fl.id, "steps")], "edges": [x.props for x in kids(fl.id, "edges")],
                                   "params": [x.props for x in kids(fl.id, "params")], "tests": [x.props for x in kids(fl.id, "tests")]})
                      for fl in kids(mn.id, "flows")]}))
    sec = f.nodes[SEC].props
    return AppModel(
        schema_version=schema_version,  # type: ignore[arg-type]
        application=ApplicationInfo.model_validate(f.nodes[APP].props),
        modules=modules,
        security=SecurityModel.model_validate({**sec, "access": [with_id(r) for r in kids(SEC, "access")]}),
        lint_suppressions=[LintSuppression.model_validate(n.props) for n in kids(ROOT, "lint")],
    )


# --- merge ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Conflict:
    id: str
    kind: str  # property | delete-edit | add-add | orphan | order
    node: str
    element_type: str
    name: str
    prop: str | None
    base: Any
    ours: Any
    theirs: Any
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "element_id": self.node, "element_type": self.element_type,
                "name": self.name, "property": self.prop, "base": self.base, "ours": self.ours, "theirs": self.theirs,
                "message": self.message}


@dataclass
class MergeResult:
    model: AppModel | None
    conflicts: list[Conflict]  # unresolved
    all_conflicts: list[Conflict] = field(default_factory=list)
    merged_from_both: int = 0  # elements where both sides changed different things and were combined

    @property
    def clean(self) -> bool:
        return self.model is not None


SET_PROPS = {("enum", "values"), ("access", "operations"), ("security", "roles"), ("application", "locales")}
DICT_PROPS = {("entity", "hooks"), ("security", "pages"), ("step", "config")}
LAYOUT_PROPS = {("step", "position")}  # never a conflict: ours wins


def _eq(a: Any, b: Any) -> bool:
    if a is _ABSENT or b is _ABSENT:
        return a is b
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class _Merger:
    def __init__(self, base: Flat, ours: Flat, theirs: Flat, resolutions: Mapping[str, str]) -> None:
        self.base, self.ours, self.theirs, self.res = base, ours, theirs, resolutions
        self.conflicts: list[Conflict] = []
        self.both = 0
        self.nodes: dict[str, Node] = {}

    # -- helpers
    def conflict(self, kind: str, n: Node, prop: str | None, b: Any, o: Any, t: Any, msg: str, suffix: str = "") -> Conflict:
        c = Conflict(f"{n.id}#{prop or kind}{suffix}", kind, n.id, n.kind, n.label, prop, b, o, t, msg)
        self.conflicts.append(c)
        return c

    def choice(self, c: Conflict) -> str | None:
        pick = self.res.get(c.id)
        return pick if pick in ("ours", "theirs", "keep", "drop") else None  # anything else leaves it unresolved

    # -- property level
    def merge_prop(self, node: Node, prop: str, b: Any, o: Any, t: Any) -> Any:
        """Three-way merge of one property; ``b`` may be a sentinel for 'absent'."""
        if _eq(o, t):
            return o
        if _eq(o, b):
            return t
        if _eq(t, b):
            return o
        key = (node.kind, prop)
        if key in LAYOUT_PROPS:
            return o
        if key in SET_PROPS and all(isinstance(x, list) for x in (b, o, t)):
            added = [x for x in t if x not in b]
            removed = [x for x in b if x not in t]
            self.both += 1
            return [x for x in o if x not in removed] + [x for x in added if x not in o]
        if key in DICT_PROPS and all(isinstance(x, dict) for x in (b, o, t)):
            out: dict[str, Any] = {}
            for k in [*o, *[k for k in t if k not in o], *[k for k in b if k not in o and k not in t]]:
                bv, ov, tv = b.get(k, _ABSENT), o.get(k, _ABSENT), t.get(k, _ABSENT)
                if _eq(ov, tv):
                    v = ov
                elif _eq(ov, bv):
                    v = tv
                elif _eq(tv, bv):
                    v = ov
                else:
                    c = self.conflict("property", node, f"{prop}.{k}", _show(bv), _show(ov), _show(tv),
                                      f"both branches changed {prop}.{k} of {node.kind} '{node.label}' differently")
                    pick = self.choice(c)
                    v = tv if pick == "theirs" else ov
                if v is not _ABSENT:
                    out[k] = v
            self.both += 1
            return out
        c = self.conflict("property", node, prop, _show(b), _show(o), _show(t),
                          f"both branches changed {prop} of {node.kind} '{node.label}' differently")
        return t if self.choice(c) == "theirs" else o

    def merge_node(self, nid: str) -> Node | None:
        b, o, t = self.base.nodes.get(nid), self.ours.nodes.get(nid), self.theirs.nodes.get(nid)
        same = lambda x, y: (x is None and y is None) or (x is not None and y is not None and x.parent == y.parent and _eq(x.props, y.props))  # noqa: E731
        if same(o, t):
            return o
        if b is None:
            if o is None:
                return t
            if t is None:
                return o
            # added on both sides with different content: compare property by property against nothing
            merged = {}
            for k in [*o.props, *[k for k in t.props if k not in o.props]]:
                merged[k] = self.merge_prop(o, k, _ABSENT, o.props.get(k, _ABSENT), t.props.get(k, _ABSENT))
            return Node(o.id, o.kind, o.parent, o.slot, {k: v for k, v in merged.items() if v is not _ABSENT})
        if o is None or t is None:
            gone, kept = ("ours", t) if o is None else ("theirs", o)
            if same(kept, b):
                return None  # deleted on one side, untouched on the other
            c = self.conflict("delete-edit", b, None, _show(b.props), _show(o.props if o else None), _show(t.props if t else None),
                              f"{b.kind} '{b.label}' was deleted on {gone} branch but edited on the other")
            return t if self.choice(c) == "theirs" else o
        # present on all three: merge parent and properties
        if o.parent != t.parent:
            if o.parent == b.parent:
                parent, slot = t.parent, t.slot
            elif t.parent == b.parent:
                parent, slot = o.parent, o.slot
            else:
                c = self.conflict("property", o, "parent", b.parent, o.parent, t.parent, f"{o.kind} '{o.label}' was moved to different places")
                parent, slot = (t.parent, t.slot) if self.choice(c) == "theirs" else (o.parent, o.slot)
        else:
            parent, slot = o.parent, o.slot
        merged: dict[str, Any] = {}
        changed_o = {k for k in {*o.props, *b.props} if not _eq(o.props.get(k, _ABSENT), b.props.get(k, _ABSENT))}
        changed_t = {k for k in {*t.props, *b.props} if not _eq(t.props.get(k, _ABSENT), b.props.get(k, _ABSENT))}
        if changed_o and changed_t and not (changed_o & changed_t):
            self.both += 1
        for k in [*o.props, *[k for k in t.props if k not in o.props], *[k for k in b.props if k not in o.props and k not in t.props]]:
            v = self.merge_prop(o, k, b.props.get(k, _ABSENT), o.props.get(k, _ABSENT), t.props.get(k, _ABSENT))
            if v is not _ABSENT:
                merged[k] = v
        return Node(o.id, o.kind, parent, slot, merged)

    # -- structure
    def run(self) -> Flat:
        ids = list(dict.fromkeys([*self.ours.nodes, *self.theirs.nodes, *self.base.nodes]))
        for nid in ids:
            n = self.merge_node(nid)
            if n is not None:
                self.nodes[nid] = n
        # a child whose parent did not survive is an orphan
        changed = True
        while changed:
            changed = False
            for nid, n in list(self.nodes.items()):
                if n.parent in (None, ROOT) or n.parent in self.nodes:
                    continue
                c = self.conflict("orphan", n, None, None, _show(n.props), None,
                                  f"{n.kind} '{n.label}' was kept or added but its parent was deleted on the other branch")
                if self.choice(c) == "keep":
                    for side in (self.ours, self.theirs):
                        p = side.nodes.get(n.parent or "")
                        if p is not None:
                            self.nodes[p.id] = p
                            break
                else:
                    del self.nodes[nid]
                changed = True
        out = Flat(nodes=self.nodes)
        slots = {(n.parent, n.slot) for n in self.nodes.values() if n.parent is not None and n.slot is not None}
        for parent, slot in sorted(slots, key=str):
            out.order[(parent, slot)] = self.merge_order(parent, slot)  # type: ignore[arg-type]
        return out

    def merge_order(self, parent: str, slot: str) -> list[str]:
        result = self._order_core(parent, slot)
        # Every surviving node needs a position, including ones a resolution brought back that the chosen
        # list does not mention: put each after its predecessor in the first list that has it.
        lists = [self.theirs.order.get((parent, slot), []), self.ours.order.get((parent, slot), []),
                 self.base.order.get((parent, slot), [])]
        for i, n in self.nodes.items():
            if n.parent == parent and n.slot == slot and i not in result:
                for lst in lists:
                    if i in lst:
                        k = lst.index(i)
                        pred = next((x for x in reversed(lst[:k]) if x in result), None)
                        result.insert(result.index(pred) + 1 if pred else 0, i)
                        break
                else:
                    result.append(i)
        return result

    def _order_core(self, parent: str, slot: str) -> list[str]:
        b = [i for i in self.base.order.get((parent, slot), [])]
        o = [i for i in self.ours.order.get((parent, slot), [])]
        t = [i for i in self.theirs.order.get((parent, slot), [])]
        alive = lambda ids: [i for i in ids if i in self.nodes and self.nodes[i].parent == parent and self.nodes[i].slot == slot]  # noqa: E731
        if o == b or o == t:  # we did not touch this list (or both agree): take theirs as it is
            return alive(t)
        if t == b:  # they did not touch it: ours as it is
            return alive(o)
        result = alive(o)
        # Elements added on their side go after their predecessor there, behind any run of elements that
        # only we added at the same spot, so that on equal footing our additions come first.
        prev = None
        for i in alive(t):
            if i not in result:
                at = result.index(prev) + 1 if prev in result else len(result)
                while at < len(result) and result[at] not in b and result[at] not in t:
                    at += 1
                result.insert(at, i)
            prev = i
        result += [i for i in alive(b) if i not in result]  # kept (merge decided so) though absent from both lists
        common = [i for i in result if i in t and i in o]
        bo = [i for i in b if i in common]
        oo = [i for i in o if i in common]
        to = [i for i in t if i in common]
        if oo == bo and to != bo:  # only their side reordered the common elements
            it = iter(to)
            result = [next(it) if i in common else i for i in result]
        elif oo != bo and to != bo and oo != to:
            n = self.nodes.get(parent) or Node(parent, parent, None, None, {})
            c = self.conflict("order", n, slot, bo, oo, to, f"the order of {slot} in '{n.label}' was changed differently", suffix=f":{slot}")
            if self.choice(c) == "theirs":
                it = iter(to)
                result = [next(it) if i in common else i for i in result]
        return result


_ABSENT = object()


def _show(v: Any) -> Any:
    return None if v is _ABSENT else v


def merge(base: AppModel, ours: AppModel, theirs: AppModel, resolutions: Mapping[str, str] | None = None) -> MergeResult:
    """Merge ``theirs`` into ``ours`` relative to their common ancestor ``base``.

    All three models must be identified. ``resolutions`` maps conflict ids to ``"ours"`` / ``"theirs"``
    (or ``"keep"`` / ``"drop"`` for orphans). Unresolved conflicts leave ``model`` as ``None``.
    """
    m = _Merger(flatten(base), flatten(ours), flatten(theirs), resolutions or {})
    flat = m.run()
    all_conflicts = list(m.conflicts)
    unresolved = [c for c in all_conflicts if m.choice(c) is None]
    if unresolved:
        return MergeResult(None, unresolved, all_conflicts, m.both)
    try:
        return MergeResult(unflatten(flat, schema_version=ours.schema_version), [], all_conflicts, m.both)
    except ValidationError as e:
        # Each side was valid, but together they are not (for example both removed different values from
        # an enumeration and none is left). There is no choice that fixes it; someone has to edit the result.
        first = e.errors()[0]
        bad = Conflict("merged#invalid", "invalid", ROOT, "model", "merged model", ".".join(map(str, first["loc"])), None, None, None,
                       f"the combined changes are not a valid model at {'.'.join(map(str, first['loc']))}: {first['msg']}")
        return MergeResult(None, [bad], [*all_conflicts, bad], m.both)


__all__ = ["Conflict", "Flat", "MergeResult", "flatten", "merge", "unflatten"]
