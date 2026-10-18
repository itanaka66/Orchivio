# Module and template registry (FR-126)

A **module** is one module of an application model (entities, enumerations, pages, flows, processes) together with the roles, access rules
and page access it needs. A **template** is a whole starter application. Both are published as **signed, semver-versioned, immutable**
packages in the Orchivio you already run: there is no separate service. Installing a module never edits a model; it creates a **proposal**.

## 1. Publishing

`POST /applications/{id}/registry/publish` (organization admin, A3; needs `ORCHIVIO_PACKAGE_SIGNING_KEY`):

| Field | |
|---|---|
| `name` | lower-case letters, digits, `-` (2-63); a name keeps its kind forever |
| `version` | semver `1.2.3` or `1.2.3-beta.1`; every version is immutable, publish a new one to change anything |
| `kind` | `module` (give `module`: its key) or `template` (the whole revision) |
| `visibility` | `org` (members of the publishing organization) or `instance` (everyone signed in on this server) |
| `description`, `license`, `revision_id` | optional; the default revision is the current one |

A module is cut out of the model: its entities, enumerations, pages, flows and processes, the access rules on its entities, the page access of its pages
and the roles those name. The result must **validate on its own**; a module that uses another module's entities or flows is refused with the
validation issues (`not_self_contained`). The package is a stored zip with fixed order and timestamps (identical inputs, identical bytes):
`REGISTRY.json` (manifest: name, version, kind, model hash, contents, minimum runtime, SHA-256 of the model, key id), `REGISTRY.sig` (Ed25519 over the
manifest bytes, the same keys as deployment packages) and `model.zip` (the canonical archive, docs/MODEL-SPEC.md).

A package is referenced as `<organization-slug>/<name>`.

## 2. Finding and fetching

* `GET /registry/packages?q=&kind=` one row per package with its newest release (pre-releases only if there is no release), `versions` count.
* `GET /registry/packages/{org}/{name}` all versions, newest first, with contents (entities, flows, pages, roles, access-rule count) and yank state.
* `GET /registry/packages/{org}/{name}/{version}/download` the signed file (audited).
* A caller sees `instance` packages and the packages of organizations they belong to; everything else is a 404.

Version selectors: `1.2.3` exactly (also a pre-release), `^1.2.3` (>=1.2.3 <2.0.0; `^0.2.3` <0.3.0), `~1.2.3` (<1.3.0), `>=1.2.3`, `latest`. Ranges never
pick pre-releases; withdrawn (yanked) versions are never picked, not even by an exact selector.

## 3. Installing a module

`POST /applications/{id}/registry/install` `{package, version, overwrite_local_changes}` (editor):

1. the package is **verified again** from storage on every install: structure, hashes, signature against the server's trusted keys
   (`ORCHIVIO_PACKAGE_TRUSTED_PUBLIC_KEYS` plus its own key), model hash, minimum runtime;
2. the module is merged into the application's current model, together with the roles, access rules and page access it brings;
3. the result becomes a **proposal** (`Install module org/name@version`); validation, **policy packs** and the usual approval level apply, and the roles and
   permissions it adds show up in the diff as ordinary security changes;
4. the install is recorded so later upgrades can tell *unmodified* from *locally changed*.

A module key that already exists is only replaced by an **upgrade of the same package**; if the installed module was changed after installing, the
upgrade is refused (`module_modified`) unless `overwrite_local_changes` is set. A module with the same key from somewhere else is never overwritten
(`module_exists`). Element ids that would clash with the application's own are re-assigned.

`GET /applications/{id}/registry/installs` lists installed modules with their state (`installed`, `modified`, `not_applied` while the proposal is still
open) and whether a newer version exists.

## 4. Templates

`POST /projects/{id}/registry/instantiate` `{package, version, key, name}` (needs `application.create` and `model.apply`) creates an application whose first
revision is the template, under the new key and name, through the same checks (including policy packs) as creating one from a model.

## 5. Moving packages between registries

Download a package from one server and `POST /organizations/{id}/registry/import` it (raw `application/zip`) into another. It is accepted only if it is signed
by a key that server trusts, so two instances exchange packages by trusting each other's public keys; imported packages are marked `imported`.

## 6. Withdrawing

`POST /registry/packages/{org}/{name}/{version}/yank` `{reason}` (admin) stops new installs of that version; applications that already installed it
are unaffected, `unyank` reverses it. Versions are never deleted or replaced.

## 7. Not built

Removing an installed module (uninstall), dependencies between packages and automatic upgrade, renaming a module on install, a hosted public catalogue or
a federation protocol (exchange is by file), download counts and ratings, publishing from the Studio UI, and package-level licence checks (the licence
is a label).
