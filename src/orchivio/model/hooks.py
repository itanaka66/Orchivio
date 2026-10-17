"""Entity event hooks: which events exist (FR-054). Kept here so model validation and the
runtime agree without importing each other."""

HOOK_EVENTS = (
    "before_create",
    "after_create",
    "before_update",
    "after_update",
    "before_delete",
    "after_delete",
)
