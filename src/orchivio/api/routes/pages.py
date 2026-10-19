"""The page API of a running application: navigation, rows, forms and actions, enforced on the server (FR-065, FR-073).

A UI is only a convenience: everything it shows or does is decided here. For a page this means

* **navigation**: the page must list one of the caller's roles in ``security.pages`` *and* the roles must hold the entity
  operations its type implies (list/detail read, create form create, edit form read + update). Anything else looks like
  a page that does not exist;
* **fields**: a page that declares ``columns`` exposes exactly those fields, for reading and for writing;
* **actions**: an action must be declared on the page, its ``visible_expr`` must hold for the row and, for standard
  verbs, the entity operation must be allowed; ``editable_expr`` is enforced before an edit is stored. Actions bound to a
  flow run that flow as the caller, whose own data permissions still apply to every step.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from orchivio.expr import EvalError, ExprError
from orchivio.logic import FlowError, FlowRunner
from orchivio.model.schema import PageAction, PageDef, qualify
from orchivio.runtime import DataError
from orchivio.security import STANDARD_ACTIONS, page_access, resolve_access, resolve_fields

from ..deps import api_error, not_found
from .appauth import Caller, _open, _run, usable

router = APIRouter(tags=["pages"], prefix="/apps/{app_id}")


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: str | None = Field(default=None, max_length=64)


def _find(c: Caller, key: str) -> tuple[Any, PageDef]:
    """The page if it exists *and* the caller may open it; otherwise the same 404 as for a page that does not exist."""
    for m in c.model.modules:
        for p in m.pages:
            if p.key == key:
                if page_access(c.model, c.roles, p).allowed:
                    return m, p
                break
    raise not_found("page")


def _actions(page: PageDef) -> list[PageAction]:
    return [a if isinstance(a, PageAction) else PageAction(key=a) for a in page.actions]


def _allowed_here(c: Caller, entity: str, action: PageAction) -> bool:
    op = STANDARD_ACTIONS.get(action.key)
    return op is None or resolve_access(c.model, c.roles, entity, op).allowed


def _state(data, entity: str, user, row: dict[str, Any], page: PageDef, c: Caller) -> dict[str, dict[str, bool]]:
    """Which actions the user sees on this row and whether the row is editable through them."""
    out: dict[str, dict[str, bool]] = {}
    for a in _actions(page):
        if not _allowed_here(c, entity, a):
            continue
        try:
            if a.visible_expr and not data.evaluate_for_row(entity, user, row, a.visible_expr):
                continue
            editable = True if not a.editable_expr else data.evaluate_for_row(entity, user, row, a.editable_expr)
        except (EvalError, ExprError):
            continue  # an expression that cannot be evaluated hides the action rather than showing it
        out[a.key] = {"editable": editable}
    return out


_SHELL_TYPES = {"string": "string", "text": "text", "integer": "integer", "decimal": "decimal",
                "boolean": "boolean", "date": "date", "datetime": "datetime", "json": "json", "uuid": "string"}


def _fields_for_page(c: Caller, mod, page: PageDef) -> list[dict[str, Any]]:
    q = qualify(mod.key, page.entity)
    entity = next((e for e in mod.entities if e.name == page.entity), None)
    if entity is None:
        return []
    op = "create" if page.mode == "create" else ("update" if page.mode == "edit" else "read")
    write_access: frozenset[str] | None = resolve_fields(c.model, c.roles, q, op) if op != "read" else None
    fields_by_name = {f.name: f for f in entity.fields}
    cols = page.columns or [f.name for f in entity.fields if not f.primary_key]
    out = []
    for name in cols:
        f = fields_by_name.get(name)
        if f is None:
            continue
        t = f.type
        kind = _SHELL_TYPES.get(t, "string")
        if t.startswith("enum:"):
            kind = "enum"
        elif t.startswith("ref:"):
            kind = "string"
        is_ro = bool(f.generated or f.computed) or (write_access is not None and name not in write_access)
        desc: dict[str, Any] = {"name": name, "label": f.display_name or name.replace("_", " ").capitalize(),
                                 "type": kind, "required": f.required, "read_only": is_ro}
        if kind == "enum":
            target = t[5:]
            enum_def = next((e for e in mod.enumerations if e.name == target), None)
            desc["values"] = enum_def.values if enum_def else []
        if f.max_length is not None:
            desc["max_length"] = f.max_length
        out.append(desc)
    return out


def _page_json(c: Caller, mod, page: PageDef) -> dict[str, Any]:
    q = qualify(mod.key, page.entity)
    return {"key": page.key, "path": page.path, "type": page.type, "mode": page.mode, "entity": q, "layout": page.layout,
            "columns": list(page.columns), "fields": _fields_for_page(c, mod, page),
            "actions": [a.key for a in _actions(page) if _allowed_here(c, q, a)]}


@router.get("/pages")
def navigation(c: Caller = Depends(usable)) -> dict[str, Any]:
    """The menu: exactly the pages this caller may open, in model order."""
    items = [_page_json(c, m, p) for m in c.model.modules for p in m.pages if page_access(c.model, c.roles, p).allowed]
    return {"items": items}


@router.get("/pages/{key}")
def page(key: str, c: Caller = Depends(usable)) -> dict[str, Any]:
    mod, p = _find(c, key)
    return _page_json(c, mod, p)


def _columns_only(page: PageDef, item: dict[str, Any], keep_extra: tuple[str, ...] = ("_actions",)) -> dict[str, Any]:
    if not page.columns:
        return item
    allowed = set(page.columns) | set(keep_extra)
    return {k: v for k, v in item.items() if k in allowed or k == "id"}


@router.get("/pages/{key}/rows")
def rows(key: str, request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), order_by: str | None = Query(None, max_length=63),
         desc: bool = False, c: Caller = Depends(usable)) -> dict[str, Any]:
    mod, p = _find(c, key)
    if p.type != "list":
        raise api_error(409, "wrong_page_type", "only list pages have rows")
    q = qualify(mod.key, p.entity)
    data = _open(request, c)
    user = c.runtime_user
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f.")}
    if p.columns:  # a list page can only be filtered or sorted by what it shows
        if any(f not in p.columns for f in filters) or (order_by and order_by not in p.columns):
            raise api_error(422, "invalid_request", "filter and order fields must be columns of this page")

    def decorate(raw: dict[str, Any], presented: dict[str, Any]) -> dict[str, Any]:
        return {**_columns_only(p, presented, ()), "_actions": _state(data, q, user, raw, p, c)}

    return _run(lambda: data.list(q, user, filters=filters, order_by=order_by, descending=desc, limit=limit, offset=offset, decorate=decorate))


@router.get("/pages/{key}/rows/{row_id}")
def row(key: str, row_id: str, request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    mod, p = _find(c, key)
    if p.type == "list":
        raise api_error(409, "wrong_page_type", "use the list's rows or a detail/form page")
    q = qualify(mod.key, p.entity)
    data = _open(request, c)
    presented = _run(lambda: data.get(q, c.runtime_user, row_id))
    stored = _run(lambda: data.raw_row(q, c.runtime_user, row_id))
    return {**_columns_only(p, presented, ()), "_actions": _state(data, q, c.runtime_user, stored, p, c)}


def _check_fields(p: PageDef, body: dict[str, Any]) -> None:
    if p.columns:
        extra = sorted(k for k in body if k not in p.columns)
        if extra:  # the form does not offer these fields, so the server does not accept them
            raise api_error(422, "invalid_record", "the record is invalid", [{"field": f, "message": "unknown field"} for f in extra])


@router.post("/pages/{key}/rows", status_code=201)
def create_row(key: str, body: dict[str, Any], request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    mod, p = _find(c, key)
    if p.type != "form" or p.mode != "create":
        raise api_error(409, "wrong_page_type", "only a create form accepts new rows")
    _check_fields(p, body)
    q = qualify(mod.key, p.entity)
    data = _open(request, c)
    return _columns_only(p, _run(lambda: data.create(q, c.runtime_user, body)), ())


@router.patch("/pages/{key}/rows/{row_id}")
def edit_row(key: str, row_id: str, body: dict[str, Any], request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    mod, p = _find(c, key)
    if p.type != "form" or p.mode != "edit":
        raise api_error(409, "wrong_page_type", "only an edit form changes rows")
    _check_fields(p, body)
    q = qualify(mod.key, p.entity)
    data = _open(request, c)
    stored = _run(lambda: data.raw_row(q, c.runtime_user, row_id))
    edit = next((a for a in _actions(p) if a.key == "edit"), None)
    if edit is not None:
        try:
            if edit.visible_expr and not data.evaluate_for_row(q, c.runtime_user, stored, edit.visible_expr):
                raise api_error(403, "forbidden", "this record cannot be edited here")
            if edit.editable_expr and not data.evaluate_for_row(q, c.runtime_user, stored, edit.editable_expr):
                raise api_error(403, "forbidden", "this record cannot be edited here")
        except (EvalError, ExprError) as e:
            raise api_error(403, "forbidden", "this record cannot be edited here") from e
    return _columns_only(p, _run(lambda: data.update(q, c.runtime_user, row_id, body)), ())


@router.post("/pages/{key}/actions/{action}")
def invoke(key: str, action: str, body: ActionIn, request: Request, c: Caller = Depends(usable)) -> dict[str, Any]:
    mod, p = _find(c, key)
    act = next((a for a in _actions(p) if a.key == action), None)
    if act is None:
        raise not_found("action")
    q = qualify(mod.key, p.entity)
    if not _allowed_here(c, q, act):
        raise not_found("action")
    data = _open(request, c)
    user = c.runtime_user
    stored = _run(lambda: data.raw_row(q, user, body.row_id)) if body.row_id is not None else None
    if stored is not None:
        try:
            if act.visible_expr and not data.evaluate_for_row(q, user, stored, act.visible_expr):
                raise not_found("action")
        except (EvalError, ExprError) as e:
            raise not_found("action") from e
    elif act.visible_expr or act.args:
        raise api_error(422, "row_required", "this action works on a record: give row_id")
    if act.key == "delete" and act.flow is None:
        if body.row_id is None:
            raise api_error(422, "row_required", "give row_id")
        _run(lambda: data.delete(q, user, body.row_id))
        return {"status": "succeeded", "action": act.key}
    if act.flow is None:
        raise api_error(409, "no_behaviour", f"action '{act.key}' is a link or form action; use the page's rows to perform it")
    flow_q = qualify(mod.key, act.flow)
    try:
        args = {name: data.evaluate_value_for_row(q, user, stored, src) if stored is not None else None for name, src in (act.args or {}).items()}
        result = FlowRunner(data.model, data).run(flow_q, args, user)
    except (EvalError, ExprError, DataError) as e:
        raise api_error(422, "action_failed", getattr(e, "message", str(e))) from e
    except FlowError as e:
        raise api_error(422, "action_failed", e.message) from e
    return {"status": result.status, "action": act.key, "error": result.error, "steps": result.steps_executed}
