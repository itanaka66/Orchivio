# Interfaces and Extension Points

Every infrastructure dependency is reached through an interface defined in this repository, so
LLMs, queues and stores are replaceable (DESIGN.md §1.2, §5). Each interface ships with a
reference adapter that the tests use.

## 1. LLM provider — `orchivio.llm`

```python
class LLMProvider(Protocol):
    name: str
    def complete(self, request: LLMRequest) -> LLMResponse: ...
```

- `LLMRequest`: messages, model, temperature, token and time limits, optional `response_schema`,
  `prompt_template` (`name@version`, recorded for audit, DESIGN.md §7.4).
- `LLMResponse`: text, provider, model, `Usage` (tokens, cost), latency.
- Errors: `LLMError` (provider failure, never retried by the validator), `LLMTimeout`.
- `generate_validated(provider, request, PydanticModel, max_attempts=3)` parses JSON (a fenced
  block is accepted), validates against the schema and, on failure, re-prompts with the
  validation errors only. After `max_attempts` it raises `StructuredOutputError` holding every
  attempt. **Model output is data, never executed or trusted.**
- `MockProvider`: scripted `str`/`Exception` items or a responder callable (`external = False`).
- `AnthropicProvider`: Messages API over HTTPS (`httpx`, `anthropic` extra), `external = True`.
- `LLMProvider.external` tells callers that prompts leave the deployment; the API requires project
  consent before using such a provider.

Adding a provider: implement `complete`, honour `timeout_s`, map vendor errors to `LLMError`, put
the adapter in its own module and keep the vendor SDK an *optional* dependency.

## 2. Job queue — `orchivio.ports.jobs`

`submit(type, payload, max_attempts, idempotency_key)`, `get`, `cancel`, `run_next`.
Invariants: only job types with a registered handler are accepted (allowlist); a repeated
idempotency key returns the original job; a failed attempt re-queues until `max_attempts`;
cancellation of a queued job is immediate and of a running job cooperative via
`JobContext.cancelled`; terminal states are final. `InMemoryJobQueue` is the reference; the
first production adapter is DB-backed using `ai_jobs`/`builds` (open question 4 in DESIGN.md §22).

## 3. Artifact store — `orchivio.ports.storage`

`put(key, bytes) -> StoredObject(key, size, sha256)`, `get`, `exists`, `delete`. Keys are
relative, slash-separated and validated (`InvalidKey`) so they cannot escape the root;
`LocalArtifactStore` also re-checks the resolved path. Writes are atomic. An S3-compatible
adapter implements the same protocol.

## 4. Audit sink — `orchivio.ports.audit`

`record(AuditRecord)` where a record has action, actor, organization, plane, channel, target
and metadata. Must not raise for a well-formed record. The database repository currently writes
`AuditEvent` rows directly; a sink adapter over the same table is the planned unification.

## 5. Revision repository — `orchivio.db.RevisionRepository`

`create`, `get`, `list`, `diff`, `rollback`. Raises `ModelInvalid(issues)` or `NotFound`.
Callers authorize first (see ARCHITECTURE.md).

## 6. Expression schema provider — `orchivio.expr.SchemaTypes`

```python
class SchemaTypes(Protocol):
    def field_type(self, entity: str, field: str) -> Type | None: ...
    def enum_values(self, name: str) -> Sequence[str] | None: ...
```
Lets the expression engine type-check against any model source. `orchivio.model.ModelIndex`
provides the production implementation.

## 7. Policy — `orchivio.policy`

`is_allowed(role, action)` (deny by default) and `level_for_action(action)` (unknown actions
default to A4). Pure functions; the same checks must guard REST, MCP and the AI orchestrator.

## 8. AI Builder, runtime, test generation

- `orchivio.ai.AIBuilder(provider).analyze / propose_model / explain` (docs/AI-BUILDER.md).
- `orchivio.runtime.AppData(engine, RuntimeSchema, audit=...)` with `list/get/create/update/delete`,
  `PreviewStore.reset/open/drop`, and `generate()` for synthetic data (docs/RUNTIME.md).
- `orchivio.testgen.generate_cases(model)` and `run_tests(model, hash, engine, seed, rows)`.

## 9. Planned interfaces

| Interface | Purpose | Phase |
|---|---|---|
| Generator | Model → code/artifact (OpenAPI, typed clients) | 3–5 |
| Connector | Consumed REST/webhook/event destinations with allowlists | 9 |
| UI component | Typed props manifest, sandboxed rendering | 12 |
| Secret store | Resolve secret references inside the runtime only | 10 |
| MCP tool | Versioned Studio MCP tools with approval levels | 9 |

Interfaces change through an ADR when they are consumed outside this repository.
