import { useState } from 'react'
import { ApiError, type ApiClient } from '../api/client'
import { Danger, Feedback, Field, Loading, Panel, Pill, useAction, useApi, when, type Page } from './kit'

interface PackageRow { package: string; name: string; organization: string; version: string; kind: 'module' | 'template'; description: string; license: string; versions: number; contents: { modules?: string[]; entities?: string[]; roles?: string[] } }
interface VersionRow { version: string; yanked: boolean; yanked_reason: string | null; created_at: string; imported: boolean; digest: string; visibility: string }
interface Detail { package: string; kind: string; latest: string | null; versions: VersionRow[] }
interface Installed { module: string; package: string; version: string; state: string; latest_version: string | null; update_available: boolean; renamed_from: string | null; dependency_of: string | null; requires: { package: string; range: string }[]; required_by: string[] }
interface Step { package: string; version: string; module: string; action: string; dependency: boolean; renamed_from?: string | null }
interface Remote { name: string; base_url: string; token_ref: string | null; remote_organization_id: string | null }

const STATE_TEXT: Record<string, string> = { installed: 'installed', modified: 'changed after install', not_applied: 'proposal not applied yet', removal_pending: 'removal proposed' }
const list = (text: string): string[] => text.split(',').map((x) => x.trim()).filter(Boolean)

export function Registry({ client, appId, orgId, modules, canPublish }: { client: ApiClient; appId: string; orgId: string; modules: string[]; canPublish: boolean }) {
  const [tab, setTab] = useState<'installed' | 'browse' | 'publish' | 'remotes'>('installed')
  return (
    <>
      <div className="tabs" role="tablist" aria-label="Registry sections">
        {([['installed', 'Installed'], ['browse', 'Browse'], ['publish', 'Publish'], ['remotes', 'Other servers']] as const)
          .filter(([t]) => canPublish || (t !== 'publish' && t !== 'remotes'))
          .map(([t, label]) => <button key={t} type="button" role="tab" aria-selected={tab === t} className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>{label}</button>)}
      </div>
      {tab === 'installed' && <InstalledView client={client} appId={appId} />}
      {tab === 'browse' && <Browse client={client} appId={appId} />}
      {tab === 'publish' && canPublish && <Publish client={client} appId={appId} modules={modules} />}
      {tab === 'remotes' && canPublish && <Remotes client={client} orgId={orgId} />}
    </>
  )
}

function Steps({ steps }: { steps: Step[] }) {
  return (
    <ul className="list" aria-label="What will change">
      {steps.map((s) => (
        <li key={`${s.package}@${s.version}`}>
          <Pill value={s.action === 'kept' ? 'ok' : s.action === 'upgrade' ? 'warn' : 'plain'} /> {s.action} <strong>{s.package}</strong>@{s.version} as module <code>{s.module}</code>
          {s.renamed_from ? <small className="hint"> (was {s.renamed_from})</small> : null}{s.dependency ? <small className="hint"> · required</small> : null}
        </li>
      ))}
    </ul>
  )
}

function InstalledView({ client, appId }: { client: ApiClient; appId: string }) {
  const items = useApi(() => client.request<{ items: Installed[] }>('GET', `/applications/${appId}/registry/installs`), [client, appId])
  const [result, setResult] = useState<{ title: string; steps: Step[] } | null>(null)
  const [error, setError] = useState('')
  const [force, setForce] = useState<{ path: string; body: Record<string, unknown> } | null>(null)

  const propose = async (path: string, body: Record<string, unknown>) => {
    setError(''); setResult(null); setForce(null)
    try {
      const r = await client.request<{ proposal: { title: string }; steps?: Step[] }>('POST', `/applications/${appId}/registry/${path}`, body)
      setResult({ title: r.proposal.title, steps: r.steps ?? [] })
      items.reload()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'module_modified') setForce({ path, body })
      setError(e instanceof Error ? e.message : 'Failed.')
    }
  }

  return (
    <Panel title="Installed from the registry">
      <p className="hint">Installing, upgrading and removing only create a <strong>proposal</strong>; the application changes when it is approved.</p>
      <Loading loading={items.loading} error={items.error} />
      <Feedback error={error} />
      {items.data?.items.length === 0 && <p className="hint">Nothing was installed from the registry.</p>}
      <ul className="list">
        {items.data?.items.map((m) => (
          <li key={m.module} className="card slim">
            <div className="row"><strong>{m.module}</strong> <small className="hint">{m.package}@{m.version}{m.renamed_from ? ` (module ${m.renamed_from} renamed)` : ''}</small> <Pill value={m.state === 'installed' ? 'ok' : 'warn'} /> <small className="hint">{STATE_TEXT[m.state] ?? m.state}</small>
              {m.update_available && <small className="hint"> · {m.latest_version} available</small>}</div>
            {m.requires.length > 0 && <p className="hint">Requires {m.requires.map((q) => `${q.package} ${q.range}`).join(', ')}</p>}
            {m.required_by.length > 0 && <p className="hint">Required by {m.required_by.join(', ')}</p>}
            <div className="row">
              {m.update_available && <button type="button" onClick={() => void propose('install', { package: m.package, version: 'latest' })}>Upgrade to {m.latest_version}</button>}
              <Danger label="Remove…" confirm="Propose removal" onConfirm={() => void propose('uninstall', { module: m.module })} disabled={m.required_by.length > 0} />
            </div>
          </li>
        ))}
      </ul>
      {force && (
        <div role="alert" className="sev-warning">
          <p>This module was changed after it was installed. Continuing replaces or removes those changes.</p>
          <button type="button" onClick={() => void propose(force.path, { ...force.body, overwrite_local_changes: true })}>Continue anyway</button>
          <button type="button" onClick={() => setForce(null)}>Cancel</button>
        </div>
      )}
      {result && (
        <div role="status">
          <p>Proposal created: <strong>{result.title}</strong>. Review it with the other proposals.</p>
          {result.steps.length > 0 && <Steps steps={result.steps} />}
        </div>
      )}
    </Panel>
  )
}

function Browse({ client, appId }: { client: ApiClient; appId: string }) {
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('')
  const results = useApi(() => client.request<Page<PackageRow>>('GET', `/registry/packages?limit=50${q ? `&q=${encodeURIComponent(q)}` : ''}${kind ? `&kind=${kind}` : ''}`), [client, q, kind])
  const [open, setOpen] = useState<PackageRow | null>(null)
  const detail = useApi(() => (open ? client.request<Detail>('GET', `/registry/packages/${open.package}`) : Promise.resolve(null)), [client, open?.package])
  const [range, setRange] = useState('latest')
  const [rename, setRename] = useState('')
  const [outcome, setOutcome] = useState<{ title: string; steps: Step[] } | null>(null)
  const [problem, setProblem] = useState<{ message: string; details?: unknown } | null>(null)
  const [instantiate, setInstantiate] = useState({ projectId: '', key: '', name: '' })
  const [made, setMade] = useState('')

  const install = async () => {
    if (!open) return
    setOutcome(null); setProblem(null)
    try {
      const r = await client.request<{ proposal: { title: string }; steps: Step[] }>('POST', `/applications/${appId}/registry/install`, { package: open.package, version: range || 'latest', ...(rename.trim() ? { as_module: rename.trim() } : {}) })
      setOutcome({ title: r.proposal.title, steps: r.steps })
    } catch (e) { setProblem({ message: e instanceof Error ? e.message : 'Failed.', details: e instanceof ApiError ? e.details : undefined }) }
  }

  return (
    <>
      <Panel title="Find a package">
        <div className="toolbar">
          <label>Search <input aria-label="Search packages" value={q} onChange={(e) => setQ(e.target.value)} /></label>
          <label>Kind <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">any</option><option value="module">modules</option><option value="template">templates</option></select></label>
        </div>
        <Loading loading={results.loading} error={results.error} />
        {results.data?.items.length === 0 && <p className="hint">No packages match.</p>}
        <ul className="list">
          {results.data?.items.map((p) => (
            <li key={p.package} className="card slim">
              <div className="row"><strong>{p.package}</strong> <small className="hint">{p.version} · {p.kind} · {p.versions} version{p.versions === 1 ? '' : 's'}{p.license ? ` · ${p.license}` : ''}</small>
                <button type="button" onClick={() => { setOpen(p); setOutcome(null); setProblem(null); setMade('') }}>Open</button></div>
              <p className="hint">{p.description}</p>
            </li>
          ))}
        </ul>
      </Panel>

      {open && (
        <Panel title={open.package} actions={<button type="button" onClick={() => setOpen(null)}>Close</button>}>
          <Loading loading={detail.loading} error={detail.error} />
          <p>{open.description}</p>
          <p className="hint">Contains {(open.contents.entities ?? []).length} entities in {(open.contents.modules ?? []).join(', ') || '—'}; roles {(open.contents.roles ?? []).join(', ') || '—'}.</p>
          <table className="trace"><tbody>
            {detail.data?.versions.map((v) => (
              <tr key={v.version}><td>{v.version}</td><td>{v.yanked ? <><Pill value="rejected" /> withdrawn: {v.yanked_reason}</> : <Pill value="ok" />}</td><td>{when(v.created_at)}</td><td>{v.imported ? 'imported' : ''}</td></tr>
            ))}
          </tbody></table>
          {open.kind === 'module' ? (
            <form onSubmit={(e) => { e.preventDefault(); void install() }}>
              <h3>Install into this application</h3>
              <Field label="Version" hint="1.2.3, ^1.2.0, ~1.2.0, >=1.2.0 or latest"><input value={range} onChange={(e) => setRange(e.target.value)} /></Field>
              <Field label="Module key (optional)" hint="Install under another name if the module's own key is taken; page keys and routes must still be unique."><input value={rename} onChange={(e) => setRename(e.target.value)} placeholder="stock" /></Field>
              <button type="submit" className="primary">Propose installation</button>
            </form>
          ) : (
            <form onSubmit={async (e) => {
              e.preventDefault()
              try { const r = await client.request<{ application_id: string; key: string }>('POST', `/projects/${instantiate.projectId}/registry/instantiate`, { package: open.package, version: range || 'latest', key: instantiate.key, name: instantiate.name }); setMade(r.key) }
              catch (err) { setProblem({ message: err instanceof Error ? err.message : 'Failed.' }) }
            }}>
              <h3>Create an application from this template</h3>
              <Field label="Project id"><input value={instantiate.projectId} onChange={(e) => setInstantiate({ ...instantiate, projectId: e.target.value })} /></Field>
              <Field label="Application key"><input value={instantiate.key} onChange={(e) => setInstantiate({ ...instantiate, key: e.target.value })} /></Field>
              <Field label="Name"><input value={instantiate.name} onChange={(e) => setInstantiate({ ...instantiate, name: e.target.value })} /></Field>
              <button type="submit" className="primary" disabled={!instantiate.projectId || !instantiate.key || !instantiate.name}>Create application</button>
            </form>
          )}
          {problem && <div role="alert" className="sev-error"><p>{problem.message}</p>{problem.details ? <pre className="json">{JSON.stringify(problem.details, null, 2)}</pre> : null}</div>}
          {outcome && <div role="status"><p>Proposal created: <strong>{outcome.title}</strong>.</p><Steps steps={outcome.steps} /></div>}
          {made && <p role="status">Application <code>{made}</code> created.</p>}
        </Panel>
      )}
    </>
  )
}

function Publish({ client, appId, modules }: { client: ApiClient; appId: string; modules: string[] }) {
  const [form, setForm] = useState({ name: '', version: '1.0.0', kind: 'module', module: modules[0] ?? '', description: '', license: '', visibility: 'org', requires: '' })
  const [result, setResult] = useState<{ package: string; version: string } | null>(null)
  const [problem, setProblem] = useState<{ message: string; details?: unknown } | null>(null)
  const { run, error, notice } = useAction(() => undefined)
  const [yank, setYank] = useState({ ref: '', version: '', reason: '' })

  const requires = () => list(form.requires).map((r) => { const [pkg, ...rest] = r.split(/\s+/); return { package: pkg, range: rest.join(' ') || 'latest' } })

  return (
    <>
      <Panel title="Publish a module or template">
        <p className="hint">Cuts it out of the current revision and signs it. A version can never be changed afterwards; publish a new one. A module must work on its own or together with the packages it requires.</p>
        <form onSubmit={async (e) => {
          e.preventDefault(); setResult(null); setProblem(null)
          try {
            const r = await client.request<{ package: string; version: string }>('POST', `/applications/${appId}/registry/publish`, {
              name: form.name.trim(), version: form.version.trim(), kind: form.kind, ...(form.kind === 'module' ? { module: form.module, requires: requires() } : {}),
              description: form.description, license: form.license, visibility: form.visibility,
            })
            setResult(r)
          } catch (err) { setProblem({ message: err instanceof Error ? err.message : 'Failed.', details: err instanceof ApiError ? err.details : undefined }) }
        }}>
          <Field label="Name" hint="Lower-case letters, digits and -"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Version" hint="Semantic version, e.g. 1.0.0 or 1.1.0-beta.1"><input value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} /></Field>
          <Field label="Kind"><select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}><option value="module">Module</option><option value="template">Template (whole application)</option></select></Field>
          {form.kind === 'module' && (
            <>
              <Field label="Module"><select value={form.module} onChange={(e) => setForm({ ...form, module: e.target.value })}>{modules.map((m) => <option key={m} value={m}>{m}</option>)}</select></Field>
              <Field label="Requires" hint="Comma-separated: org/package range, e.g. acme/inventory-core ^1.0.0"><input value={form.requires} onChange={(e) => setForm({ ...form, requires: e.target.value })} /></Field>
            </>
          )}
          <Field label="Description"><input value={form.description} maxLength={500} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
          <Field label="Licence"><input value={form.license} onChange={(e) => setForm({ ...form, license: e.target.value })} placeholder="MIT" /></Field>
          <Field label="Visible to"><select value={form.visibility} onChange={(e) => setForm({ ...form, visibility: e.target.value })}><option value="org">my organization</option><option value="instance">everyone on this server</option></select></Field>
          <button type="submit" className="primary" disabled={!form.name.trim() || !form.version.trim() || (form.kind === 'module' && !form.module)}>Publish</button>
        </form>
        {problem && <div role="alert" className="sev-error"><p>{problem.message}</p>{problem.details ? <pre className="json">{JSON.stringify(problem.details, null, 2)}</pre> : null}</div>}
        {result && <p role="status">Published <strong>{result.package}</strong> {result.version}.</p>}
      </Panel>

      <Panel title="Withdraw a version">
        <p className="hint">A withdrawn version can no longer be installed or picked by a range. Applications that already have it keep working.</p>
        <Feedback error={error} notice={notice} />
        <form className="inline" onSubmit={(e) => { e.preventDefault(); void run(() => client.request('POST', `/registry/packages/${yank.ref}/${yank.version}/yank`, { reason: yank.reason }), 'Version withdrawn.') }}>
          <Field label="Package"><input value={yank.ref} onChange={(e) => setYank({ ...yank, ref: e.target.value })} placeholder="acme/inventory-core" /></Field>
          <Field label="Version"><input value={yank.version} onChange={(e) => setYank({ ...yank, version: e.target.value })} /></Field>
          <Field label="Reason"><input value={yank.reason} onChange={(e) => setYank({ ...yank, reason: e.target.value })} /></Field>
          <button type="submit" disabled={!yank.ref || !yank.version || !yank.reason}>Withdraw</button>
          <button type="button" disabled={!yank.ref || !yank.version} onClick={() => void run(() => client.request('POST', `/registry/packages/${yank.ref}/${yank.version}/unyank`), 'Version restored.')}>Restore</button>
        </form>
      </Panel>
    </>
  )
}

function Remotes({ client, orgId }: { client: ApiClient; orgId: string }) {
  const remotes = useApi(() => client.request<{ items: Remote[] }>('GET', `/organizations/${orgId}/registry/remotes`), [client, orgId])
  const { run, error, notice } = useAction(remotes.reload)
  const [form, setForm] = useState({ name: '', base_url: '', token_ref: '', remote_organization_id: '' })
  const [active, setActive] = useState<Remote | null>(null)
  const [q, setQ] = useState('')
  const found = useApi(() => (active ? client.request<{ items: PackageRow[] }>('GET', `/organizations/${orgId}/registry/remotes/${active.name}/packages${q ? `?q=${encodeURIComponent(q)}` : ''}`) : Promise.resolve(null)), [client, orgId, active?.name, q])
  const [msg, setMsg] = useState('')
  const [pushMsg, setPushMsg] = useState('')
  const [pushForm, setPushForm] = useState({ package: '', version: '' })

  return (
    <>
      <Panel title="Other servers' registries">
        <p className="hint">Packages move between servers as signed files over HTTPS. This server stores a pulled package only if it was signed with a key listed in <code>ORCHIVIO_PACKAGE_TRUSTED_PUBLIC_KEYS</code>; the other server checks the same when you push.</p>
        <Loading loading={remotes.loading} error={remotes.error} />
        <Feedback error={error} notice={notice} />
        <ul className="list">
          {remotes.data?.items.map((r) => (
            <li key={r.name} className="row"><strong>{r.name}</strong> <small className="hint">{r.base_url}{r.token_ref ? ` · token ${r.token_ref}` : ''}</small>
              <button type="button" onClick={() => { setActive(r); setMsg('') }}>Browse</button>
              <button type="button" className="link" onClick={() => setForm({ name: r.name, base_url: r.base_url, token_ref: r.token_ref ?? '', remote_organization_id: r.remote_organization_id ?? '' })}>Edit</button>
              <Danger label="Remove" onConfirm={() => run(() => client.request('DELETE', `/organizations/${orgId}/registry/remotes/${r.name}`), 'Removed.')} />
            </li>
          ))}
        </ul>
        <form onSubmit={(e) => { e.preventDefault(); void run(() => client.request('PUT', `/organizations/${orgId}/registry/remotes/${form.name.trim()}`, {
          base_url: form.base_url.trim(), token_ref: form.token_ref.trim() || null, remote_organization_id: form.remote_organization_id.trim() || null }), 'Saved.') }}>
          <h3>Add or change a server</h3>
          <Field label="Name"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Address" hint="https://host, without /api/v1"><input type="url" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></Field>
          <Field label="Token reference" hint="env:NAME or file:/path — an API token for that server"><input value={form.token_ref} onChange={(e) => setForm({ ...form, token_ref: e.target.value })} /></Field>
          <Field label="Their organization id" hint="Where your pushes go on that server"><input value={form.remote_organization_id} onChange={(e) => setForm({ ...form, remote_organization_id: e.target.value })} /></Field>
          <button type="submit" className="primary" disabled={!form.name.trim() || !form.base_url.trim()}>Save</button>
        </form>
      </Panel>

      {active && (
        <Panel title={`Packages on ${active.name}`} actions={<button type="button" onClick={() => setActive(null)}>Close</button>}>
          <label>Search <input aria-label="Search remote packages" value={q} onChange={(e) => setQ(e.target.value)} /></label>
          <Loading loading={found.loading} error={found.error} />
          <ul className="list">
            {found.data?.items.map((p) => (
              <li key={p.package} className="row"><strong>{p.package}</strong> <small className="hint">{p.version} · {p.kind}</small>
                <button type="button" onClick={async () => {
                  try { const r = await client.request<{ package: string; version: string }>('POST', `/organizations/${orgId}/registry/remotes/${active.name}/pull`, { package: p.package }); setMsg(`Pulled as ${r.package} ${r.version}.`) }
                  catch (e) { setMsg(e instanceof Error ? e.message : 'Failed.') }
                }}>Pull</button></li>
            ))}
          </ul>
          {msg && <p role="status">{msg}</p>}
        </Panel>
      )}

      {remotes.data && remotes.data.items.length > 0 && (
        <Panel title="Push one of your packages">
          <form className="inline" onSubmit={async (e) => {
            e.preventDefault()
            const target = remotes.data!.items[0]!.name
            try { await client.request('POST', `/organizations/${orgId}/registry/remotes/${target}/push`, pushForm); setPushMsg(`Pushed ${pushForm.package} ${pushForm.version} to ${target}.`) }
            catch (err) { setPushMsg(err instanceof Error ? err.message : 'Failed.') }
          }}>
            <Field label="Package"><input value={pushForm.package} onChange={(e) => setPushForm({ ...pushForm, package: e.target.value })} placeholder="acme/inventory-core" /></Field>
            <Field label="Version"><input value={pushForm.version} onChange={(e) => setPushForm({ ...pushForm, version: e.target.value })} /></Field>
            <button type="submit" disabled={!pushForm.package || !pushForm.version}>Push to {remotes.data.items[0]!.name}</button>
          </form>
          {pushMsg && <p role="status">{pushMsg}</p>}
        </Panel>
      )}
    </>
  )
}
