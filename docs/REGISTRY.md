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

## 5. Dependencies

A module may **require** other module packages: `requires: [{package, range}]` when publishing (`^1.0.0`, `~1.2.0`, `>=1.0.0`, an exact version or `latest`).
Publishing checks them: each must exist and be readable, its module must be part of the revision, and the module must **validate together with its
dependencies** (and what they themselves build on) even though their files are not copied into the package. The manifest records `requires` with the module key.

Installing resolves the requirements into **one proposal**, dependencies first, one version per package (the highest that matches):

* a requirement already met by an installed version is **kept** (nothing changes); an unmet one is **upgraded** (refused if that module was changed locally,
  unless `overwrite_local_changes`);
* a module key that already exists but did not come from the required package is a `dependency_conflict`, as is a second request that the chosen version does not
  satisfy; there is no backtracking, so the message names the package and who wanted what. Cycles are refused (`dependency_cycle`);
* the response lists the `steps` (install / upgrade / kept, which are dependencies, renames).

## 6. Installing under another module key

`as_module` on the first install gives the module another key. Every **qualified** reference to its own elements is rewritten (`old.Entity`, `ref:old.Entity`, access
rules, qualified flow calls, expressions); a match needs a real element name after the dot, so free text that merely contains the old key stays as it is. The
result is validated as part of the proposal. Page keys and routes are **not** changed and must be unique in the application, so a renamed copy next to the
original is refused by validation; the rename exists for a module whose key is taken by something else. Later upgrades keep the installed key
(`module_renamed` if you ask for another); a test runs the generated test suite for the original and the renamed module and requires identical outcomes.

## 7. Uninstalling

`POST /applications/{id}/registry/uninstall {module, overwrite_local_changes}` proposes removing a module that came from the registry, together with its access
rules and the page access of its pages. Roles stay (people may hold them). It is refused when another installed module requires it (`required_by`), when the module
was changed after installing (unless told otherwise), or when something else in the application still uses it (the validator says what). A revision does not drop
data tables of a deployed environment; only the model changes.

## 8. Other servers' registries

Packages move between servers as signed files over HTTPS; there is no federation protocol and no trust in a remote's word. Per organization an admin registers a
**remote**: `PUT /organizations/{id}/registry/remotes/{name}` `{base_url, token_ref, remote_organization_id}` (the server's address without `/api/v1`; a bearer
token for it as an `env:` / `file:` reference; where pushes go). Then:

* `GET .../remotes/{name}/packages?q=` searches what that server shows to the token;
* `POST .../remotes/{name}/pull {package, version}` downloads a package and stores it as an **imported** package of this organization, **only if it is signed with a
  key in `ORCHIVIO_PACKAGE_TRUSTED_PUBLIC_KEYS`** (same verification as an import);
* `POST .../remotes/{name}/push {package, version}` sends one of your own packages to the remote's import endpoint, which applies the same check with its keys.

Addresses go through the SSRF-pinned transport and the same destination policy as webhooks (https, public addresses, optional allow-list); the remote's error text is never
passed on (only fixed messages: unreachable, refused our credentials, not found, rejected it, it already has it). Trusting another server therefore means adding its public
key, nothing more.

## 9. Studio

*Module registry* in the administration workspace (editors can browse and install; admins publish and manage remotes): installed modules with state (installed,
changed after install, proposal not applied, removal proposed), requirements, updates and one-click upgrade/removal proposals; search with versions and withdrawn
versions; installation with a version range and optional module key showing the resolved steps; templates; publishing with requirements; withdrawing and restoring a
version; remote registries (browse, pull, push).

## 10. Not built

Automatic updates, dependency ranges with alternatives (`||`) or backtracking resolution, a rename that also changes page keys and routes, optional/peer dependencies,
mirroring or federation between registries, download counts and ratings, signature key rotation tooling (add the new key to the trusted list), and licence checks
(the licence is a label).
