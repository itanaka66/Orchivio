import { useEffect, useState } from 'react'
import { ApiError, type ApiClient } from '../api/client'
import { Danger, Feedback, Field, Loading, Panel, useAction, useApi, when } from './kit'

interface Sync { remote_url: string; base_branch: string; push_branch: string; path: string; token_ref: string | null; provider: string; last_push_at: string | null; last_push_commit: string | null; last_pull_at: string | null; last_pull_commit: string | null }
interface PushResult { changed: boolean; commit: string | null; branch: string; pull_request: string | null; pull_request_error: string | null; note: string | null }

const blank = { remote_url: '', base_branch: 'main', push_branch: '', path: '', token_ref: '', provider: 'git' }

export function GitSync({ client, appId }: { client: ApiClient; appId: string }) {
  const cfg = useApi(async () => {
    try { return await client.request<Sync>('GET', `/applications/${appId}/git-sync`) } catch (e) { if (e instanceof ApiError && e.status === 404) return null; throw e }
  }, [client, appId])
  const { run, error, notice } = useAction(cfg.reload)
  const [form, setForm] = useState(blank)
  const [pr, setPr] = useState(false)
  const [result, setResult] = useState<PushResult | null>(null)
  const [pulled, setPulled] = useState('')
  const [opError, setOpError] = useState('')

  useEffect(() => {
    const c = cfg.data
    setForm(c ? { remote_url: c.remote_url, base_branch: c.base_branch, push_branch: c.push_branch, path: c.path, token_ref: c.token_ref ?? '', provider: c.provider } : blank)
  }, [cfg.data])

  const op = async (work: () => Promise<void>) => {
    setOpError(''); setResult(null); setPulled('')
    try { await work(); cfg.reload() } catch (e) { setOpError(e instanceof Error ? e.message : 'Failed.') }
  }

  return (
    <>
      <Panel title="Git repository">
        <p className="hint">Orchivio pushes a revision to its own branch (and can open a pull request); people review and merge in Git; <em>Pull</em> turns the merged branch into a proposal. Git never changes the model directly.</p>
        <Loading loading={cfg.loading} error={cfg.error} />
        <Feedback error={error} notice={notice} />
        <form onSubmit={(e) => {
          e.preventDefault()
          void run(() => client.request('PUT', `/applications/${appId}/git-sync`, {
            remote_url: form.remote_url.trim(), base_branch: form.base_branch.trim(), push_branch: form.push_branch.trim() || null, path: form.path.trim(),
            token_ref: form.token_ref.trim() || null, provider: form.provider,
          }), 'Saved.')
        }}>
          <Field label="Repository URL" hint="https://host/owner/repo — the host must be allowed on the server"><input type="url" value={form.remote_url} onChange={(e) => setForm({ ...form, remote_url: e.target.value })} /></Field>
          <div className="kv-row">
            <Field label="Base branch"><input value={form.base_branch} onChange={(e) => setForm({ ...form, base_branch: e.target.value })} /></Field>
            <Field label="Push branch" hint="Default orchivio/<app>"><input value={form.push_branch} onChange={(e) => setForm({ ...form, push_branch: e.target.value })} /></Field>
          </div>
          <Field label="Folder in the repository" hint="Empty = the repository root"><input value={form.path} onChange={(e) => setForm({ ...form, path: e.target.value })} /></Field>
          <Field label="Token reference" hint="env:NAME or file:/path"><input value={form.token_ref} onChange={(e) => setForm({ ...form, token_ref: e.target.value })} /></Field>
          <Field label="Provider"><select value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}><option value="git">Plain Git (push only)</option><option value="github">GitHub (pull requests)</option></select></Field>
          <div className="toolbar">
            <button type="submit" className="primary" disabled={!form.remote_url.trim()}>Save</button>
            {cfg.data && <Danger label="Disconnect" onConfirm={() => run(() => client.request('DELETE', `/applications/${appId}/git-sync`), 'Disconnected.')} />}
          </div>
        </form>
      </Panel>

      {cfg.data && (
        <Panel title="Synchronise">
          <p className="hint">Last push: {when(cfg.data.last_push_at)}{cfg.data.last_push_commit ? ` (${cfg.data.last_push_commit.slice(0, 7)})` : ''} · last pull: {when(cfg.data.last_pull_at)}{cfg.data.last_pull_commit ? ` (${cfg.data.last_pull_commit.slice(0, 7)})` : ''}</p>
          <label className="field check"><input type="checkbox" checked={pr} disabled={cfg.data.provider !== 'github'} onChange={(e) => setPr(e.target.checked)} /><span>Open a pull request into {cfg.data.base_branch}</span></label>
          <div className="toolbar">
            <button type="button" className="primary" onClick={() => void op(async () => setResult(await client.request<PushResult>('POST', `/applications/${appId}/git-sync/push`, { open_pull_request: pr && cfg.data?.provider === 'github' })))}>Push current revision</button>
            <button type="button" onClick={() => void op(async () => { const p = await client.request<{ title: string }>('POST', `/applications/${appId}/git-sync/pull`); setPulled(p.title) })}>Pull {cfg.data.base_branch} as a proposal</button>
          </div>
          {opError && <p role="alert" className="sev-error">{opError}</p>}
          {result && (
            <div role="status">
              <p>{result.changed ? `Pushed ${result.commit?.slice(0, 7)} to ${result.branch}.` : result.note}</p>
              {result.pull_request && <p>Pull request: <a href={result.pull_request} target="_blank" rel="noreferrer noopener">{result.pull_request}</a></p>}
              {result.pull_request_error && <p className="sev-warning">The branch was pushed, but the pull request failed: {result.pull_request_error}</p>}
            </div>
          )}
          {pulled && <p role="status">Proposal created: {pulled}. Review it with the other proposals.</p>}
        </Panel>
      )}
    </>
  )
}
