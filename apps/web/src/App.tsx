import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiClient, ApiError, type Application, type Org, type Project, type User } from './api/client'
import { FlowEditor } from './components/FlowEditor'
import { FLOW_KEY_RE } from './flow/catalog'
import { listFlows, viewOf, withFlow } from './flow/model'
import { newFlow } from './flow/ops'
import sampleModel from './flow/sample-model.json'
import type { Flow } from './flow/types'

type Json = Record<string, unknown>

interface Open { model: Json; module: string; flow: Flow; application?: Application; readOnly: boolean; demo: boolean }

export default function App({ client: injected }: { client?: ApiClient }) {
  const client = useMemo(() => injected ?? new ApiClient(), [injected])
  const [user, setUser] = useState<User | null>(null)
  const [booting, setBooting] = useState(client.signedIn)
  const [open, setOpen] = useState<Open | null>(null)

  useEffect(() => {
    if (!client.signedIn) return
    client.me().then((m) => setUser(m.user), () => undefined).finally(() => setBooting(false))
  }, [client])

  if (booting) return <main className="page"><p>Loading…</p></main>

  if (open) {
    const { model, module, flow, application, readOnly, demo } = open
    const view = viewOf(model, module)
    return (
      <div className="shell">
        <header className="topbar">
          <button type="button" className="link" onClick={() => setOpen(null)}>← All flows</button>
          <span>{application ? `${application.name} · ` : demo ? 'Demo · ' : ''}{module}</span>
          <span className="spacer" />
          {user && <span>{user.display_name}</span>}
        </header>
        <FlowEditor
          key={`${module}.${flow.key}`}
          flow={flow}
          model={view}
          readOnly={readOnly}
          validate={user ? (f) => client.validateFlow(model, module, f) : undefined}
          onSave={application && user && !readOnly ? async (f) => {
            const p = await client.createProposal(application.id, withFlow(model, module, f), `Edit flow ${f.key}`)
            return `Proposal created (${p.approval_level}, ${p.diff.changes.length} change${p.diff.changes.length === 1 ? '' : 's'}). It takes effect when someone with the right role approves it.`
          } : undefined}
        />
      </div>
    )
  }

  return (
    <main className="page">
      <h1>Orchivio Studio · Flow builder</h1>
      {user ? (
        <Browser client={client} user={user} onOpen={setOpen}
                 onLogout={() => { void client.logout().finally(() => setUser(null)) }} />
      ) : (
        <Login client={client} onUser={setUser} />
      )}
      <section aria-label="Demo">
        <h2>Try it without a server</h2>
        <p>Opens the example “Reorder low stock” flow from the sample inventory app. Sign in first to get live validation from the server.</p>
        <button type="button" onClick={() => {
          const f = listFlows(sampleModel as Json)[0]
          if (f) setOpen({ model: sampleModel as Json, module: f.module, flow: f.flow, readOnly: false, demo: true })
        }}>Open the demo flow</button>
      </section>
    </main>
  )
}

function Login({ client, onUser }: { client: ApiClient; onUser: (u: User) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  return (
    <form className="card" onSubmit={async (e) => {
      e.preventDefault()
      setBusy(true)
      setError('')
      try { onUser(await client.login(email, password)) }
      catch (err) { setError(err instanceof ApiError ? err.message : 'Could not reach the server.') }
      finally { setBusy(false) }
    }}>
      <h2>Sign in</h2>
      <label className="field"><span>Email</span><input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
      <label className="field"><span>Password</span><input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      {error && <p role="alert" className="sev-error">{error}</p>}
      <button type="submit" className="primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
    </form>
  )
}

function useLoad<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: string } {
  const [state, setState] = useState<{ data: T | null; error: string }>({ data: null, error: '' })
  useEffect(() => {
    let live = true
    setState({ data: null, error: '' })
    load().then((d) => live && setState({ data: d, error: '' }), (e) => live && setState({ data: null, error: e instanceof Error ? e.message : 'Failed to load.' }))
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return state
}

function Browser({ client, user, onOpen, onLogout }: { client: ApiClient; user: User; onOpen: (o: Open) => void; onLogout: () => void }) {
  const orgs = useLoad(() => client.organizations(), [client])
  const [org, setOrg] = useState<Org | null>(null)
  const projects = useLoad(() => (org ? client.projects(org.id) : Promise.resolve(null)), [org])
  const [project, setProject] = useState<Project | null>(null)
  const apps = useLoad(() => (project ? client.applications(project.id) : Promise.resolve(null)), [project])
  const [app, setApp] = useState<Application | null>(null)
  const revision = useLoad(() => (app?.current_revision_id ? client.revision(app.current_revision_id) : Promise.resolve(null)), [app])
  const [newKey, setNewKey] = useState('')
  const [error, setError] = useState('')

  const flows = revision.data ? listFlows(revision.data.model_json) : []
  const canEdit = org ? ['owner', 'admin', 'editor'].includes(org.role) : false
  const modules = revision.data ? ((revision.data.model_json.modules ?? []) as { key: string }[]) : []

  const create = useCallback(() => {
    const mod = modules[0]?.key
    if (!revision.data || !mod) return
    if (!FLOW_KEY_RE.test(newKey)) return setError('Flow names start with a letter and use letters, digits and underscores.')
    if (flows.some((f) => f.module === mod && f.flow.key === newKey)) return setError('A flow with that name already exists.')
    setError('')
    onOpen({ model: revision.data.model_json, module: mod, flow: newFlow(newKey), application: app ?? undefined, readOnly: !canEdit, demo: false })
  }, [modules, revision.data, newKey, flows, onOpen, app, canEdit])

  return (
    <section className="card">
      <p>Signed in as <strong>{user.display_name}</strong>. <button type="button" className="link" onClick={onLogout}>Sign out</button></p>
      <Pick label="Organization" items={orgs.data?.items} error={orgs.error} value={org?.id} onPick={(id) => { setOrg(orgs.data?.items.find((o) => o.id === id) ?? null); setProject(null); setApp(null) }} />
      {org && <Pick label="Project" items={projects.data?.items} error={projects.error} value={project?.id} onPick={(id) => { setProject(projects.data?.items.find((p) => p.id === id) ?? null); setApp(null) }} />}
      {project && <Pick label="Application" items={apps.data?.items} error={apps.error} value={app?.id} onPick={(id) => setApp(apps.data?.items.find((a) => a.id === id) ?? null)} />}
      {app && !app.current_revision_id && <p className="hint">This application has no model yet.</p>}
      {revision.error && <p role="alert" className="sev-error">{revision.error}</p>}
      {revision.data && (
        <>
          <h2>Flows in revision {revision.data.revision_no}</h2>
          {flows.length === 0 && <p className="hint">No flows yet.</p>}
          <ul className="list">
            {flows.map((f) => (
              <li key={`${f.module}.${f.flow.key}`}>
                <button type="button" onClick={() => onOpen({ model: revision.data!.model_json, module: f.module, flow: f.flow, application: app ?? undefined, readOnly: !canEdit, demo: false })}>
                  {f.flow.key} <small>({f.flow.kind}, {f.flow.steps.length} steps)</small>
                </button>
              </li>
            ))}
          </ul>
          {canEdit && (
            <form onSubmit={(e) => { e.preventDefault(); create() }}>
              <label className="field"><span>New flow name</span><input value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="NotifyLowStock" /></label>
              <button type="submit" disabled={!newKey}>Create flow</button>
              {error && <p role="alert" className="sev-error">{error}</p>}
            </form>
          )}
        </>
      )}
    </section>
  )
}

function Pick<T extends { id: string; name: string }>({ label, items, error, value, onPick }: {
  label: string; items?: T[]; error: string; value?: string; onPick: (id: string) => void
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value ?? ''} onChange={(e) => onPick(e.target.value)} disabled={!items}>
        <option value="">{items ? `Choose ${label.toLowerCase()}…` : 'Loading…'}</option>
        {items?.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
      </select>
      {error && <small role="alert" className="sev-error">{error}</small>}
    </label>
  )
}
