# Governance console (FR-124)

Organization-wide controls and reporting. Everything here needs the admin-level `governance.manage` (organization
owners and admins; classified A3). Applications and projects of one organization are never visible to another.

## 1. Policy packs

A **pack** is a named list of rules from a fixed catalogue (`GET /governance/policy-rules`). There are no expressions and no
code: each rule has parameters and a severity, `error` (blocks) or `warning` (reported only).

| Rule | Parameters | Meaning |
|---|---|---|
| `no-anonymous-access` | none | `security.anonymous` must stay off |
| `integration-destinations` | `allowed: [names]` | integrations may only use these destination names (none = no integrations) |
| `forbid-operations` | `operations`, `roles` or `"*"` | access rules may not grant these operations (`read`, `create`, `update`, `delete`, `export`) |
| `require-row-constraint` | `operations`, `entities` or `"*"`, `exempt_roles` | access rules granting these operations need a row constraint |
| `max-roles` | `max` | at most this many application roles |
| `forbid-automation-roles` | `roles` | schedules and process system tasks may not run as these roles |
| `limit-flow-run-roles` | `roles` | only these roles may appear in flows' `run_roles` |

`POST/GET/PATCH/DELETE /organizations/{id}/policy-packs[/{pack}]`. A pack can be `active` or `disabled`; all active packs of the
organization apply to all its applications.

**Enforcement.** Packs are evaluated wherever a model is planned (`RevisionRepository.plan`, and the merge path), so every way of
changing an application meets the same rules: revisions, proposals (human, AI and MCP), imports, branches, merges and rollbacks.
A violation appears as an issue `policy:<rule>` with the path and a message; `error` issues make the change fail like a validation
error (`422 model_invalid` / `merge_invalid`), `warning` issues do not. Rules run only after ordinary validation passes. A pack
never rewrites existing revisions; creating one does not break running applications.

## 2. Compliance, inventory, AI usage, audit export

* `GET /organizations/{id}/governance/compliance`: every application's current model against the active packs
  (`compliant`, `warnings`, `violations`, `no_model`) with each violation and its pack. Shows what the next change to an application
  must fix.
* `GET /organizations/{id}/governance/inventory`: every application with revision, counts (entities, flows, processes, integrations,
  pages, end users, active webhooks, open proposals, environments with a live deployment) and **risk flags** that deserve a second
  look: `anonymous-access`, `export-granted`, `external-integrations`, `flows-runnable-by-users`, `scheduled-flows`,
  `outbound-webhooks`, `oidc-auto-provisioning`, `external-ai-allowed`. It is a summary: no URLs, secrets or data.
* `GET /organizations/{id}/governance/ai-usage?from&to`: AI Builder jobs in a window (default 30 days) with counts, status, tokens, cost
  and MCP tool calls, by project, job type and provider/model, as reported by the providers. Windows over 100 000 jobs are refused.
* `GET /organizations/{id}/governance/audit-export?from&to&plane&action_prefix&limit&after_id&format=jsonl|csv`: the organization's audit
  events, oldest first, up to 50 000 per request, for archives or a SIEM. Headers: `X-Row-Count`, `X-Last-Id` and `X-More` (page with
  `after_id`), `X-Content-SHA256` of the body. CSV has a BOM and prefixes cells that start with `= + - @` with a quote. `action_prefix` is
  literal (`%` and `_` are not wildcards). **Every export is itself an audit event** (`audit.exported`: filters, row count, hash).

## 3. Not built

A governance UI, per-project packs or exceptions with expiry, spending limits or quotas, scheduled or streaming exports, signed or
hash-chained audit archives (the per-export hash only detects changes to a downloaded file), and policy rules over data or flows'
internals.
