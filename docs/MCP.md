# Studio MCP Server

The Studio exposes a [Model Context Protocol](https://modelcontextprotocol.io/) server that lets
AI agents (Claude Code, Claude Desktop, and any MCP client) read and propose changes to
application models through the same permission and audit pipeline as the Studio UI.

Implementation: `src/orchivio/api/routes/mcp.py`. Requirements: DESIGN.md §7.5 (FR-086).

> **Status.** The MCP server is implemented and compatible with MCP protocol versions `2025-06-18`
> and `2025-03-26`. All ten tools are available. External MCP client tooling (agents embedded in
> generated applications) is planned for Phase 9 (R0.3) and is not yet built.

---

## Endpoint

```
POST /api/v1/mcp
GET  /api/v1/mcp   (capability discovery / health)
```

JSON-RPC 2.0 over Streamable HTTP (request/response only; no SSE stream). The server announces
itself as `orchivio-studio/<version>`.

---

## Authentication

Bearer API tokens only. Session cookies are deliberately refused on this endpoint — a page
script cannot abuse the MCP server even if the Studio is open.

### Creating a token

1. Sign in to the Studio.
2. Open your organisation's **Settings → API tokens**.
3. Click **New token**, choose a name, select scopes, and optionally scope to one project.
4. Copy the token immediately — it is shown only once (prefix `oat_`).

The token must have at least the scopes required by the tools you call (see the Tools table).
A `*` scope grants everything. Tokens cannot mint further tokens (`session_required` error).

### Using the token

```
Authorization: Bearer oat_<token>
Content-Type: application/json
```

### Rate limiting

Defaults to 60 calls per token per minute. Exceeded calls receive a `429` JSON-RPC error.

---

## Tools

Every tool validates its arguments strictly (unknown keys are rejected). Read tools (`A0`) are
safe to run without human confirmation. Write tools (`A1`) create proposals — they never apply
changes directly.

| Tool | Level | Required scope | Description |
|---|---|---|---|
| `application.list` | A0 | `project.read` | List the applications of a project |
| `model.read` | A0 | `model.read` | Read a full model or one element by stable id |
| `model.search` | A0 | `model.read` | Find elements by name or kind; returns stable ids |
| `model.usages` | A0 | `model.read` | Find elements that depend on an element |
| `model.validate` | A0 | — | Validate and lint a model document without storing it |
| `model.lint` | A0 | `model.read` | Stored lint findings of a revision |
| `proposal.create` | A1 | `model.propose` | Submit a model as a pending proposal |
| `proposal.get` | A0 | `model.read` | Read a proposal's status, diff, risk level and decision |
| `proposal.list` | A0 | `model.read` | List proposals of an application |
| `tests.run` | A1 | `model.propose` | Run generated tests in an isolated preview database |

**Approval levels:**

| Level | Meaning |
|---|---|
| A0 | Read, explain, summarise — may run automatically |
| A1 | Non-destructive proposals — generation is allowed; *application* requires human approval |

`proposal.create` never applies a proposal. A person must open the Studio, review the diff, and
approve. Agents cannot raise their own approval level.

---

## Governance

- Every call is audited with the tool name, a SHA-256 hash of the arguments, and the outcome.
  Audit records are available at `GET /api/v1/audit` (admin scope).
- Arguments are validated with Pydantic (`extra="forbid"`); unknown keys return a JSON-RPC
  `INVALID_PARAMS` error, not a silent ignore.
- Tool failures return `isError: true` in the result so the agent can read the error message;
  protocol failures return a JSON-RPC error object.
- Results larger than 200 000 characters are truncated with a `truncated: true` payload and
  `isError: true`. Use `element_id`, `query`, or `limit` to narrow the request.

---

## Example: read → propose workflow (Claude Code)

Add to your MCP client configuration (e.g. `.mcp.json` for Claude Code):

```json
{
  "mcpServers": {
    "orchivio": {
      "type": "http",
      "url": "http://localhost:8000/api/v1/mcp",
      "headers": { "Authorization": "Bearer oat_<your-token>" }
    }
  }
}
```

Typical agentic workflow:

1. `application.list` — find the application id.
2. `model.read` — read the current model. **Keep element ids stable.**
3. Modify the model in memory; run `model.validate` to check before submitting.
4. `proposal.create` — submit the modified model. Returns the proposal id and diff summary.
5. A person reviews and approves (or rejects) in the Studio.

Starting `proposal.create` from `model.read` output preserves stable element ids, which
prevents unnecessary renames in the diff.

---

## External MCP servers (consumed by the AI Builder)

The Studio can also *call* external MCP servers on behalf of the AI Builder (registered under
**Settings → MCP Servers**, managed with `mcpserver.manage` scope). This is distinct from the
Studio MCP server described above and is implemented at `src/orchivio/api/routes/mcpservers.py`.
