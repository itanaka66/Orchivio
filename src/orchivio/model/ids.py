"""Stable ``element_id`` assignment (DESIGN.md section 8.8)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

from .schema import AppModel, Element


def iter_elements(model: AppModel) -> Iterator[tuple[tuple[str, ...], Element]]:
    """Yield ``(natural_key, element)`` for every identified element, parents first."""
    for mod in model.modules:
        yield ("module", mod.key), mod
        for en in mod.enumerations:
            yield ("enum", mod.key, en.name), en
        for ent in mod.entities:
            yield ("entity", mod.key, ent.name), ent
            for f in ent.fields:
                yield ("field", mod.key, ent.name, f.name), f
        for pg in mod.pages:
            yield ("page", mod.key, pg.key), pg
    for rule in model.security.access:
        yield ("access", rule.entity, rule.role), rule


def assign_element_ids(
    model: AppModel,
    previous: AppModel | None = None,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> AppModel:
    """Return a copy of ``model`` with every element identified.

    Existing ids are kept. Elements without one inherit the id of the same-named element
    in ``previous`` (so a client that drops ids does not turn edits into delete+create),
    otherwise a fresh id is generated. To express a rename, keep the ``element_id``.
    """
    out = model.model_copy(deep=True)
    known: dict[tuple[str, ...], Any] = {}
    if previous is not None:
        known = {key: el.element_id for key, el in iter_elements(previous) if el.element_id}
    used = {el.element_id for _, el in iter_elements(out) if el.element_id}
    for key, el in iter_elements(out):
        if el.element_id:
            continue
        candidate = known.get(key)
        if candidate and candidate not in used:
            el.element_id = candidate
        else:
            el.element_id = new_id()
        used.add(el.element_id)
    return out
