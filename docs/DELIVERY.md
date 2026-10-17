# Delivery: environments, signed packages, promotion

Covers FR-094 (environments and promotion), FR-095 (immutable signed packages) and FR-096 (constants and secret
references). **There is no deployment target yet**: a deployment is the platform's authoritative record of which
signed package is live in which stage and of every gate and approval behind it. Running that package on
infrastructure (and zero-downtime switchover, FR-097) is not built.

## Environments

Per application, ordered by `stage_order` (typically dev, test, prod). Each has `requires_approval` (default
false) and `require_tests` (default true). Managing them needs the admin-level `delivery.manage`.

Constants (`PUT /environments/{id}/constants/{KEY}`) are either a plain `value` or a `secret_ref`
(`env:`, `vault:`, `file:`, `aws-sm:`, `gcp-sm:`, `azure-kv:` + path). Secrets are never stored: a key whose name
looks like a secret (`password`, `token`, `api_key`...) refuses a plain value, and the audit log records only the
key and kind.

## Packages

`POST /revisions/{id}/packages` (editor, `package.build`) builds a zip containing

| File | Content |
|---|---|
| `model.zip` | canonical model archive (docs/MODEL-SPEC.md §9) |
| `migration-plan.json` | expand/contract steps from the model deployed before (see below) |
| `sbom.json` | CycloneDX-style list of the runtime's direct dependencies and installed versions |
| `PACKAGE.json` | runtime version, schema version, model hash, previous model hash, signing key id, SHA-256 of every file |
| `PACKAGE.sig` | Ed25519 signature over the exact bytes of `PACKAGE.json` |

* **Reproducible.** Fixed entry order and timestamps, no compression, sorted JSON: the same revision, runtime
  version, base and SBOM give byte-identical output, so building again returns the existing package (`200`
  instead of `201`). The SBOM reflects the installed environment, so it is stable per installation.
* **Signed.** The signature covers the manifest and the manifest covers every file, so any changed byte fails
  verification. Generate a key with `orchivio keygen`, set `ORCHIVIO_PACKAGE_SIGNING_KEY` (the secret seed) on
  the server only, and verify anywhere with just the public key: `orchivio verify-package FILE --public-key K`.
  `ORCHIVIO_PACKAGE_TRUSTED_PUBLIC_KEYS` adds keys (for example from a previous signing key) the server accepts.
* **Verified on every use.** The server re-reads the stored bytes and checks digest, hashes and signature on
  `GET /packages/{id}`, `/verify`, `/download`, when promoting and when approving.
* Downloading is admin-only (`project.export`) because the package contains the whole model.

### Migration plan

Elements are matched by stable id, so a rename is *add column, copy, drop old column* and never data loss.
Steps carry `phase` and `destructive`:

* `expand` (additive; may run before the new version is live): `create_table`, `add_column` (with
  `backfill_required` when the column is required and has no default), `rename_table`.
* `contract` (after the new version serves traffic; never undone by rolling code back): `drop_column`,
  `drop_table`, `alter_column_type`, `narrow_length` (destructive), `set_not_null` (existing rows must be checked).

The plan starts from the model of `previous_package_id` (the package currently deployed where this one is
headed), or from nothing for a first deployment.

## Promotion

`POST /packages/{id}/promote {environment, acknowledge_risks?, comment?}` (editor, `deployment.request`). Gates,
checked in this order, each failing with its own `409` code:

1. `package_corrupt`: the stored package must verify.
2. `stage_skipped`: the stage before must currently run *this* package.
3. `already_deployed` / `already_pending`.
4. `tests_required`: when the environment requires tests, the revision needs a succeeded test run.
5. `migration_base_mismatch`: the plan's starting model must equal the model deployed in the target now.
   To roll back or skip ahead, build a package with `previous_package_id` set to what is deployed.
6. `approval_required`: destructive migration steps need `acknowledge_risks: true`.
7. Approval policy: without `requires_approval` the deployment goes live at once; otherwise it is
   `pending_approval` until `POST /deployments/{id}/approval {decision}` by an admin **other than the requester**
   (`403 separation_of_duties`). Approval re-verifies the package and the migration base.

Going live marks the previous deployment of that environment `superseded`. Requests, approvals and rejections
are audited (`package.built`, `deployment.requested|deployed|rejected`, `environment.*`).

## Not built

A deployment executor, run-time resolution of `secret_ref`, package upload from another installation, version
channels, per-environment AI budgets (FR-105), and a Studio UI for any of this.
