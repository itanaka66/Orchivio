import { useState } from 'react'
import { ApiError, type ApiClient } from '../api/client'
import { Danger, Feedback, Field, Json, Loading, Panel, Pill, useAction, useApi, when, type Page } from './kit'

interface Env { id: string; name: string; stage_order: number; requires_approval: boolean; require_tests: boolean; current: { status: string; package_id: string } | null; current_package: { model_hash: string } | null }
interface Pkg { id: string; revision_id: string; runtime_version: string; model_hash: string; digest: string; created_at: string }
interface Deployment { id: string; environment_id: string; package_id: string; status: string; comment: string | null; created_at: string; requested_by: string | null }
interface Constant { key: string; value: string | null; secret_ref: string | null }

export function Delivery({ client, appId, currentRevisionId }: { client: ApiClient; appId: string; currentRevisionId: string | null }) {
  const envs = useApi(() => client.request<{ items: Env[] }>('GET', `/applications/${appId}/environments`), [client, appId])
  const pkgs = useApi(() => client.request<Page<Pkg>>('GET', `/applications/${appId}/packages?limit=20`), [client, appId])
  const deps = useApi(() => client.request<Page<Deployment>>('GET', `/applications/${appId}/deployments?limit=20`), [client, appId])
  const reloadAll = () => { envs.reload(); pkgs.reload(); deps.reload() }
  const { run, error, notice } = useAction(reloadAll)
  const [env, setEnv] = useState({ name: '', stage_order: '0', requires_approval: false, require_tests: true })
  const [constantsOf, setConstantsOf] = useState<Env | null>(null)
  const [promote, setPromote] = useState<{ pkg: Pkg; environment: string; acknowledge: boolean; comment: string } | null>(null)
  const [promoteError, setPromoteError] = useState<{ message: string; steps?: unknown } | null>(null)
  const [detail, setDetail] = useState<unknown>(null)

  const envName = (id: string) => envs.data?.items.find((e) => e.id === id)?.name ?? id.slice(0, 8)

  const doPromote = async () => {
    if (!promote) return
    setPromoteError(null)
    try {
      await client.request('POST', `/packages/${promote.pkg.id}/promote`, { environment: promote.environment, acknowledge_risks: promote.acknowledge, comment: promote.comment || undefined })
      setPromote(null)
      reloadAll()
    } catch (e) {
      if (e instanceof ApiError) setPromoteError({ message: e.message, steps: (e.details as { destructive_steps?: unknown } | undefined)?.destructive_steps })
      else setPromoteError({ message: 'Promotion failed.' })
    }
  }

  return (
    <>
      <Panel title="Environments">
        <Loading loading={envs.loading} error={envs.error} />
        <Feedback error={error} notice={notice} />
        <table className="trace">
          <thead><tr><th>Stage</th><th>Name</th><th>Live</th><th>Gates</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {envs.data?.items.map((e) => (
              <tr key={e.id}>
                <td>{e.stage_order}</td><td>{e.name}</td>
                <td>{e.current ? <><Pill value={e.current.status} /> <small className="hint">{e.current_package?.model_hash.slice(0, 10)}</small></> : <span className="hint">nothing deployed</span>}</td>
                <td>{e.requires_approval ? 'approval' : 'automatic'}{e.require_tests ? ' · tests' : ''}</td>
                <td>
                  <button type="button" className="link" onClick={() => setConstantsOf(constantsOf?.id === e.id ? null : e)}>Constants</button>
                  <button type="button" className="link" onClick={() => void run(() => client.request('PATCH', `/environments/${e.id}`, { requires_approval: !e.requires_approval }))}>{e.requires_approval ? 'Skip approval' : 'Require approval'}</button>
                  <Danger label="Delete" onConfirm={() => run(() => client.request('DELETE', `/environments/${e.id}`), `${e.name} deleted.`)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <form className="inline" onSubmit={(ev) => { ev.preventDefault(); void run(() => client.request('POST', `/applications/${appId}/environments`, { name: env.name.trim(), stage_order: Number(env.stage_order), requires_approval: env.requires_approval, require_tests: env.require_tests }), 'Environment created.').then(() => setEnv({ ...env, name: '' })) }}>
          <Field label="New environment"><input value={env.name} placeholder="test" onChange={(e) => setEnv({ ...env, name: e.target.value })} /></Field>
          <Field label="Stage order"><input type="number" min={0} max={100} value={env.stage_order} onChange={(e) => setEnv({ ...env, stage_order: e.target.value })} /></Field>
          <label className="field check"><input type="checkbox" checked={env.requires_approval} onChange={(e) => setEnv({ ...env, requires_approval: e.target.checked })} /><span>Needs approval</span></label>
          <label className="field check"><input type="checkbox" checked={env.require_tests} onChange={(e) => setEnv({ ...env, require_tests: e.target.checked })} /><span>Needs passing tests</span></label>
          <button type="submit" disabled={!env.name.trim()}>Add</button>
        </form>
      </Panel>

      {constantsOf && <Constants client={client} env={constantsOf} />}

      <Panel title="Packages" actions={
        <button type="button" className="primary" disabled={!currentRevisionId}
          onClick={() => void run(() => client.request('POST', `/revisions/${currentRevisionId}/packages`, {}), 'Package built (or found: identical inputs give the same package).')}>Build package of current revision</button>}>
        <Loading loading={pkgs.loading} error={pkgs.error} />
        {pkgs.data?.items.length === 0 && <p className="hint">No packages yet.</p>}
        <ul className="list">
          {pkgs.data?.items.map((p) => (
            <li key={p.id} className="card slim">
              <div className="row"><code>{p.digest.slice(0, 16)}</code> <small className="hint">runtime {p.runtime_version} · model {p.model_hash.slice(0, 10)} · {when(p.created_at)}</small></div>
              <div className="row">
                <button type="button" onClick={async () => { try { setDetail(await client.request('GET', `/packages/${p.id}/verify`)) } catch (e) { setDetail({ valid: false, error: e instanceof Error ? e.message : 'failed' }) } }}>Verify</button>
                <button type="button" onClick={async () => setDetail(await client.request('GET', `/packages/${p.id}`))}>Migration plan &amp; SBOM</button>
                {/* FR-034: run expand migration dry-run */}
                <button type="button" onClick={async () => setDetail(await client.request('POST', `/packages/${p.id}/migrations/run`, { phase: 'expand', dry_run: true }))}>Preview migration</button>
                {/* FR-034: apply expand migration */}
                <button type="button" onClick={() => void run(() => client.request('POST', `/packages/${p.id}/migrations/run`, { phase: 'expand', dry_run: false }), 'Expand migration applied.')}>Run expand migration</button>
                {/* FR-036: export package */}
                <a href={client.url(`/packages/${p.id}/download`)}>Export package</a>
                <button type="button" className="primary" onClick={() => { setPromoteError(null); setPromote({ pkg: p, environment: envs.data?.items[0]?.name ?? '', acknowledge: false, comment: '' }) }}>Promote…</button>
              </div>
            </li>
          ))}
        </ul>
        {detail !== null && <Json value={detail} />}
      </Panel>

      {promote && (
        <Panel title={`Promote ${promote.pkg.digest.slice(0, 12)}`}>
          <Field label="Environment"><select value={promote.environment} onChange={(e) => setPromote({ ...promote, environment: e.target.value })}>
            {envs.data?.items.map((e) => <option key={e.id} value={e.name}>{e.name}</option>)}</select></Field>
          <Field label="Comment"><input value={promote.comment} onChange={(e) => setPromote({ ...promote, comment: e.target.value })} /></Field>
          <label className="field check"><input type="checkbox" checked={promote.acknowledge} onChange={(e) => setPromote({ ...promote, acknowledge: e.target.checked })} /><span>I accept destructive migration steps, if any</span></label>
          {promoteError && <div role="alert" className="sev-error"><p>{promoteError.message}</p>{promoteError.steps ? <Json value={promoteError.steps} /> : null}</div>}
          <div className="toolbar"><button type="button" className="primary" disabled={!promote.environment} onClick={() => void doPromote()}>Request deployment</button><button type="button" onClick={() => setPromote(null)}>Cancel</button></div>
        </Panel>
      )}

      <Panel title="Deployments">
        <Loading loading={deps.loading} error={deps.error} />
        <ul className="list">
          {deps.data?.items.map((d) => (
            <li key={d.id} className="row">
              <strong>{envName(d.environment_id)}</strong> <Pill value={d.status} /> <small className="hint">{when(d.created_at)}{d.comment ? ` · ${d.comment}` : ''}</small>
              {d.status === 'pending_approval' && (
                <>
                  <button type="button" onClick={() => void run(() => client.request('POST', `/deployments/${d.id}/approval`, { decision: 'approve' }), 'Approved.')}>Approve</button>
                  <button type="button" onClick={() => void run(() => client.request('POST', `/deployments/${d.id}/approval`, { decision: 'reject' }), 'Rejected.')}>Reject</button>
                </>
              )}
            </li>
          ))}
        </ul>
      </Panel>
    </>
  )
}

const SECRETISH = /(secret|password|passwd|token|api[_-]?key|private|credential)/i

function Constants({ client, env }: { client: ApiClient; env: Env }) {
  const items = useApi(() => client.request<{ items: Constant[] }>('GET', `/environments/${env.id}/constants`), [client, env.id])
  const { run, error, notice } = useAction(items.reload)
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [ref, setRef] = useState(false)
  const secretish = SECRETISH.test(key)
  return (
    <Panel title={`Constants of ${env.name}`}>
      <p className="hint">Values here are not secret. Anything credential-like must be a reference such as env:NAME; it is read on the server when needed.</p>
      <Loading loading={items.loading} error={items.error} />
      <Feedback error={error} notice={notice} />
      <table className="trace"><tbody>
        {items.data?.items.map((c) => (
          <tr key={c.key}><td><code>{c.key}</code></td><td>{c.secret_ref ? <span className="chip">ref {c.secret_ref}</span> : c.value}</td>
            <td><Danger label="Delete" onConfirm={() => run(() => client.request('DELETE', `/environments/${env.id}/constants/${encodeURIComponent(c.key)}`))} /></td></tr>
        ))}
      </tbody></table>
      <form className="inline" onSubmit={(e) => { e.preventDefault(); void run(() => client.request('PUT', `/environments/${env.id}/constants/${encodeURIComponent(key.trim())}`, ref ? { secret_ref: value.trim() } : { value }), 'Saved.').then(() => { setKey(''); setValue('') }) }}>
        <Field label="Key"><input value={key} onChange={(e) => setKey(e.target.value)} /></Field>
        <Field label={ref ? 'Secret reference' : 'Value'} hint={secretish && !ref ? 'This name looks like a secret: use a reference.' : undefined}><input value={value} onChange={(e) => setValue(e.target.value)} placeholder={ref ? 'env:PAYMENTS_TOKEN' : ''} /></Field>
        <label className="field check"><input type="checkbox" checked={ref} onChange={(e) => setRef(e.target.checked)} /><span>Secret reference</span></label>
        <button type="submit" disabled={!key.trim() || !value.trim()}>Save</button>
      </form>
    </Panel>
  )
}
