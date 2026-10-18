# Git synchronisation (FR-093)

An application's model is already stored as one file per element (docs/MODEL-SPEC.md). Git sync moves those files to and from a Git
repository so that changes can be reviewed as pull requests. **Git never changes a model directly**: what comes back is a proposal.

## 1. The flow

```
Orchivio revision --push--> branch orchivio/<app>  --pull request--> base branch (main)  --pull--> Orchivio proposal --approve--> revision
```

* **Push** (`POST /applications/{id}/git-sync/push`, admin): writes the revision's canonical files under the configured path as one commit on
  the push branch, which belongs to Orchivio and is force-updated. The base branch is never written. Other files in the repository are
  preserved; files of the canonical format under the path that no longer exist in the model are removed. With `open_pull_request` (provider
  `github`) a pull request into the base branch is opened, or the open one is reused. If the base branch or the push branch already holds exactly
  this model, nothing is pushed (`changed: false`).
* **Pull** (`POST .../git-sync/pull`, editor): reads the base branch, rebuilds the model and creates a pending proposal titled
  `Git sync: <branch>@<sha>`. It goes through the usual validation, policy packs and approval levels. `409 no_changes` when the repository equals the
  current model; `422 invalid_repository_files` / `model_invalid` when it does not read or validate.

## 2. Configuration

`PUT /applications/{id}/git-sync` (admin, A3), also `GET` and `DELETE`:

| Field | Meaning |
|---|---|
| `remote_url` | `https://host/owner/repo(.git)`; no credentials, query or fragment |
| `base_branch` | branch people review and merge into (default `main`); pulled from |
| `push_branch` | branch Orchivio pushes to (default `orchivio/<app key>`); must differ from the base branch |
| `path` | sub-directory of the repository that holds the model (default: the root) |
| `token_ref` | `env:NAME` or `file:/path` holding a token; resolved at call time, never stored or returned |
| `provider` | `git` or `github` (needed for pull requests) |

Server settings: `ORCHIVIO_GIT_SYNC_ALLOWED_HOSTS` (fnmatch patterns such as `github.com,*.corp.example`; **empty disables Git sync**),
`ORCHIVIO_GIT_SYNC_TIMEOUT_SECONDS` (60), `ORCHIVIO_GIT_EXECUTABLE` (`git`), `ORCHIVIO_GIT_GITHUB_API_URL` (`https://api.github.com`, or
`https://ghe.example.com/api/v3`). The server needs `git` installed. A token needs push rights on the push branch (and pull-request rights for
`github`), and read rights for pulling private repositories.

## 3. How the remote is kept at arm's length

The repository is untrusted and the URL is set by people with admin rights, so:

* only `https` remotes on the operator's allow-listed hosts that resolve to public addresses (a wildcard allow-list still never reaches private
  addresses); no URL credentials; redirects off; no `ssh`, `file`, `ext::` or other transports (`GIT_ALLOW_PROTOCOL=https`);
* git runs with an empty home, no system or user config, prompts off, hooks disabled, `fsckObjects` on; the token goes into the environment as an
  HTTP header (never an argument or URL) and is scrubbed from any error text;
* no working tree is ever checked out. Files are read with `ls-tree`/`cat-file` and written with a private index (`hash-object`, `update-index`,
  `write-tree`, `commit-tree`), so symlinks, submodules, filters and hooks from the remote cannot act;
* pulling accepts regular files only (a symlink or submodule at a path of the format is refused), with the import limits on count and size, and
  ignores files that are not shaped like the format (README, docs, CI). Reading the files that are is strict, as for archives;
* git's output is never shown: failures map to fixed sentences (credentials refused, not found, push refused, unreachable);
* one sync per application at a time; every configure, push and pull is audited (no token, no URLs of the remote beyond the configured one).

**Residual risk.** git resolves the host name itself, so the "public address" check cannot be pinned the way webhooks are; a DNS-rebinding
attack against an allow-listed name is only as hard as compromising that name. Keep the allow-list to hosts you trust, and run the server
without network access to internal services you would not want a Git host to reach.

## 4. Not built

Webhook-triggered pulls (pull is a button/API call), GitLab/Bitbucket pull-request APIs (the `git` provider pushes only), SSH remotes and deploy
keys, signed commits, per-user authorship (commits are authored by "Orchivio"; the audit trail records who pushed), merging a diverged push branch
(it is force-updated), partial/sparse sync of branches of the application, and a Studio screen (API only).
