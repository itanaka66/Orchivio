# Branch / Merge Semantics and Canonical File Format

Covers FR-088 (branches), FR-089 (three-way semantic merge), FR-090 (canonical Git files), and
FR-091 (merge conflict resolution UI). Requirements: DESIGN.md §4.9 and §8.9.

> **Status.** Branches and merge are **planned for Release 0.4 (Phase 10)**. The current
> implementation has a linear revision history with parent IDs (`parent_revision_ids`) and a
> canonical model archive format (see `docs/MODEL-SPEC.md §9`). Nothing in this document is
> implemented yet unless explicitly noted.

---

## 1. Revision graph

Each revision already stores `parent_revision_ids: list[UUID]` (one for a normal edit, two for a
merge commit). This is the foundation for branches.

A **branch** is a named pointer to a revision. The initial (and currently only) branch is
`main`. Creating a branch is cheap — it records a name and the revision it points to.

```
main:    R1 ─── R2 ─── R3
                         \
feature/add-orders:       R4 ─── R5
```

---

## 2. Three-way semantic merge

Because the model is a structured document (not free text), merges operate on model elements,
not lines.

### 2.1 Algorithm (planned)

1. Find the **merge base**: the most recent common ancestor revision of the two branch tips.
2. Compute `diff(base, ours)` and `diff(base, theirs)` using the same element-id-stable diff
   engine that drives proposals.
3. Merge:
   - Element touched by one side only → take that side's version.
   - Element touched by both sides identically → no conflict.
   - Element touched by both sides differently → **conflict** (both versions recorded,
     resolution required).
   - Element deleted on one side, modified on the other → always a conflict.
4. Produce a merge revision with both tips as parents and the merged model as its model.

### 2.2 Conflicts

Each conflict is a `MergeConflict` record with:

| Field | Description |
|---|---|
| `element_id` | Stable id of the element in conflict |
| `kind` | Element kind (e.g. `field`, `page`, `rule`) |
| `ours` | The element as it stands on the current branch |
| `theirs` | The element as it stands on the incoming branch |
| `base` | The element at the merge base |

The Studio merge UI shows conflicts side-by-side. The resolver picks `ours`, `theirs`, or edits
manually. All conflicts must be resolved before a merge revision can be created.

### 2.3 Approval

A merge that introduces an A3+ change (e.g. new external call, permission downgrade) requires
the same approval as any other proposal at that level.

---

## 3. Canonical file format

Each revision can be exported as a **canonical model archive** (`model.zip`) whose content is
stable given the same model. The format is documented in `docs/MODEL-SPEC.md §9`.

The canonical archive is the basis for:

- **Git sync** (planned): push the canonical files to a Git repository branch after each
  approved revision; pull / open a PR to import a revision from Git.
- **Signed packages** (implemented): `docs/DELIVERY.md` — the package includes `model.zip` and
  a manifest signed with Ed25519.
- **Three-way merge** (planned): check out two branch tips and the merge base as canonical
  archives; diff at the file level as a fast path before semantic diffing.

### 3.1 Archive layout (canonical order)

```
model.zip
├── meta.json          { schema_version, revision_id, revision_no, model_hash, created_at }
├── modules/
│   └── <key>/
│       ├── module.json
│       ├── entities/
│       │   └── <name>.json
│       ├── enumerations/
│       │   └── <name>.json
│       ├── pages/
│       │   └── <key>.json
│       ├── flows/
│       │   └── <key>.json
│       └── processes/
│           └── <key>.json
└── security.json      (if present)
```

Files use sorted keys, no trailing whitespace, and Unix newlines. Entry order inside the zip
is deterministic (sorted path). The same revision, runtime version, and installed SBOM produce
byte-identical archives.

---

## 4. Git sync (planned)

When Git sync is enabled for an application (`gitsync.manage` scope, `docs/GIT-SYNC.md`):

- Each approved revision is committed to a configured Git branch as canonical files.
- A push or PR to that branch imports the changes as a proposal for human review.
- Conflict resolution follows §2 above.

---

## 5. Invariants preserved across merges

- Every element id in the merged model must be unique.
- The merged model must pass schema validation and lint at the same level as a normal proposal.
- Stable ids (`element_id` on every node) must not be reassigned; a conflict that would require
  reassignment is escalated to a manual resolution.
- The merged model's `parent_revision_ids` contains both branch tips (in canonical order by id).
