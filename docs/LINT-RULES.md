# Model Lint Rules

Generated from `orchivio.model.lint` (`catalog_markdown()`); do not edit by hand.

Error findings block a revision unless suppressed with `lint_suppressions` (`rule`, `element_id`, `justification` of at least 10 characters).

| Rule | Severity | Category | What it flags |
|---|---|---|---|
| `sec-constraint-not-compilable` | error | security | **Row constraint cannot be compiled to SQL.** The runtime cannot translate this constraint to a query filter, so it would deny every request. |
| `sec-constraint-on-nullable-field` | warning | security | **Row constraint uses a nullable field.** Null-safe comparisons cannot use a plain index and a missing value can silently widen or narrow access. |
| `sec-unconstrained-write` | warning | security | **Write access without a row constraint on user-owned data.** The entity has a reference to the user but a role can change any user's rows. |
| `sec-write-without-read` | warning | security | **Role can write but not read.** A role that modifies records it cannot read is usually a mistake. |
| `sec-anonymous-access` | warning | security | **Anonymous access enabled.** Anonymous access exposes pages without authentication and needs A3 approval. |
| `sec-superuser-role` | info | security | **Role with unrestricted access to everything.** A role that can do every operation on every entity without constraints is effectively an administrator. |
| `sec-sensitive-field-listed` | warning | security | **Sensitive-looking field shown in a list page.** Field names such as password, secret or token should not be rendered in list views. |
| `perf-unindexed-constraint-field` | warning | performance | **Row-constraint field without an index.** Row constraints run on every query; the fields they filter on should be indexed (DESIGN.md 5.1). |
| `perf-reference-without-index` | info | performance | **Reference field without an index.** Joins and cascades on unindexed reference fields get slower as data grows. |
| `perf-unique-nullable-field` | warning | performance | **Unique constraint on a nullable field.** SQL databases allow multiple NULL values in a UNIQUE column, so the uniqueness guarantee does not apply to rows where this field is absent. Mark the field required or use a partial index. |
| `perf-wide-entity` | info | performance | **Entity with very many fields.** Entities with more than 40 fields are hard to maintain and slow to list. |
| `perf-wide-list-page` | info | performance | **List page with many columns.** More than 12 columns is rarely readable and loads more data than needed. |
| `maint-unused-enumeration` | warning | maintainability | **Enumeration is never used.** No field or expression refers to this enumeration. |
| `maint-unused-entity` | info | maintainability | **Entity has no pages and is not referenced.** No page, reference field or expression uses the entity (access rules do not count). |
| `maint-missing-display-name` | info | maintainability | **Entity without a display name.** Display names make the generated UI and messages readable. |
| `maint-single-value-enumeration` | warning | maintainability | **Enumeration with one value.** A single-valued enumeration carries no information. |
| `maint-duplicate-list-page` | info | maintainability | **Several list pages for one entity.** Usually one list page per entity is enough. |
| `maint-no-creation-timestamp` | info | maintainability | **Entity has no creation timestamp.** A generated datetime field makes records auditable and sortable. |
| `maint-long-expression` | info | maintainability | **Very long expression.** Expressions over 300 characters are hard to review; consider computed fields. |
| `maint-access-on-transient-entity` | warning | maintainability | **Access rule on a transient entity.** Transient entities are never stored, so access rules on them have no effect. |
| `maint-unreachable-flow` | warning | maintainability | **Server flow with no known entry point.** No page action, hook, schedule, run_roles, or call_flow step refers to this flow, so it can never run. |
| `maint-process-no-start-roles` | warning | maintainability | **Process with no start roles and no call_process reference.** A process that has no start_roles and is not called as a sub-process by any other process can never be started. |
| `a11y-column-without-label` | warning | accessibility | **Displayed field without a display name.** Fields shown in pages need a human-readable label for screen readers and translation. |
| `i18n-default-locale-not-listed` | warning | accessibility | **Default locale missing from locales.** When locales are declared, the default locale must be one of them. |
