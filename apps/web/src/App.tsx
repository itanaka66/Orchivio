import { useCallback, useEffect, useMemo, useState, type ComponentProps, type ReactNode } from 'react'
import { ApiClient, ApiError, type Application, type Org, type Project, type User } from './api/client'
import { AdminWorkspace } from './admin/AdminWorkspace'
import { Board } from './board/Board'
import { collectElements } from './board/elements'
import { FeedbackWidget } from './board/FeedbackWidget'
import { FlowEditor } from './components/FlowEditor'
import { remember, saveModel, PROFILE_KEY } from './offline'
import { OfflineGate } from './offline/OfflineGate'
import { OfflinePanel } from './offline/OfflinePanel'
import { OfflineProvider, useOffline } from './offline/OfflineProvider'
import { Preview } from './preview/Preview'
import { PwaBar } from './pwa/PwaBar'
import { clearShellCaches } from './pwa/register'
import { FLOW_KEY_RE } from './flow/catalog'
import { listFlows, viewOf, withFlow } from './flow/model'
import { newFlow } from './flow/ops'
import sampleModel from './flow/sample-model.json'
import type { Flow } from './flow/types'

type Json = Record<string, unknown>

interface BoardTarget { project: Project; application?: Application; elements: { id: string; label: string }[]; canEdit: boolean }
interface AdminTarget { org: Org; project: Project; application?: Application; model: Json | null }
interface PreviewTarget { application: Application; model: Json; roles: string[]; canEdit: boolean }
interface Open { model: Json; module: string; flow: Flow; application?: Application; readOnly: boolean; demo: boolean }

export default function App({ client: injected, offline }: { client?: ApiClient; offline?: Omit<ComponentProps<typeof OfflineProvider>, 'client' | 'children'> }) {
  const client = useMemo(() => injected ?? new ApiClient(), [injected])
  return <OfflineProvider client={client} {...offline}><Studio client={client} /></OfflineProvider>
}

function Studio({ client }: { client: ApiClient }) {
  const off = useOffline()
  const [user, setUser] = useState<User | null>(null)
  const [booting, setBooting] = useState(client.signedIn)
  const [offlineStart, setOfflineStart] = useState(false) // signed in before, but the server cannot be reached
  const [open, setOpen] = useState<Open | null>(null)
  const [board, setBoard] = useState<BoardTarget | null>(null)
  const [preview, setPreview] = useState<PreviewTarget | null>(null)
  const [admin, setAdmin] = useState<AdminTarget | null>(null)

  useEffect(() => {
    if (!client.signedIn) return
    client.me().then((m) => setUser(m.user), (e: unknown) => { if (e instanceof TypeError) setOfflineStart(true) }).finally(() => setBooting(false))
  }, [client])

  // offline data belongs to one account; remember who is signed in, and a name to greet them with while offline
  useEffect(() => { off?.bindUser(user?.id ?? null) }, [off, user?.id])
  useEffect(() => { if (user && off?.unlocked) void off.snapshot(PROFILE_KEY, { displayName: user.display_name }) }, [user, off?.unlocked]) // eslint-disable-line react-hooks/exhaustive-deps

  const signOut = () => {
    if (off && off.pending > 0 && !window.confirm(`${off.pending} change${off.pending === 1 ? ' is' : 's are'} not sent yet. They stay encrypted on this device and are sent after you sign in again. Sign out now?`)) return
    off?.lock()
    void client.logout().finally(() => { clearShellCaches(); setUser(null) })
  }

  if (booting) return <main className="page"><p>Loading…</p></main>

  // The browser below stays mounted (hidden) while a screen is open, so going back keeps the chosen organization, project and application.
  let overlay: ReactNode = null
  if (admin) {
    overlay = (
      <div className="shell">
        <header className="topbar">
          <button type="button" className="link" onClick={() => setAdmin(null)}>← Back</button>
          <span>{admin.project.name}{admin.application ? ` · ${admin.application.name}` : ''} · Administration</span>
          <span className="spacer" />
          {user && <span>{user.display_name}</span>}
        </header>
        <main className="page wide"><AdminWorkspace client={client} org={admin.org} project={admin.project} application={admin.application} model={admin.model} /></main>
      </div>
    )

  } else if (preview) {
    overlay = (
      <div className="shell">
        <header className="topbar">
          <button type="button" className="link" onClick={() => setPreview(null)}>← Back</button>
          <span>{preview.application.name} · Preview</span>
          <span className="spacer" />
          {user && <span>{user.display_name}</span>}
        </header>
        <main className="page wide">
          <Preview client={client} applicationId={preview.application.id} model={preview.model} roles={preview.roles} canEdit={preview.canEdit} />
        </main>
      </div>
    )

  } else if (board) {
    overlay = (
      <div className="shell">
        <header className="topbar">
          <button type="button" className="link" onClick={() => setBoard(null)}>← Back</button>
          <span>{board.project.name}{board.application ? ` · ${board.application.name}` : ''} · Story board</span>
          <span className="spacer" />
          {user && <span>{user.display_name}</span>}
        </header>
        <main className="page wide" data-orchivio-page="studio:board">
          <Board client={client} projectId={board.project.id} applicationId={board.application?.id} elements={board.elements} canEdit={board.canEdit} />
        </main>
        {board.application && <FeedbackWidget client={client} applicationId={board.application.id} page="studio:board" />}
      </div>
    )

  } else if (open) {
    const { model, module, flow, application, readOnly, demo } = open
    const view = viewOf(model, module)
    overlay = (
      <div className="shell">
        <header className="topbar">
          <button type="button" className="link" onClick={() => setOpen(null)}>← All flows</button>
          <span>{application ? `${application.name} · ` : demo ? 'Demo · ' : ''}{module}</span>
          <span className="spacer" />
          {user && <span>{user.display_name}</span>}
        </header>
        {application && user && <FeedbackWidget client={client} applicationId={application.id} page={`studio:flow:${module}.${flow.key}`} />}
        <FlowEditor
          key={`${module}.${flow.key}`}
          flow={flow}
          model={view}
          readOnly={readOnly}
          validate={user ? (f) => client.validateFlow(model, module, f) : undefined}
          run={user && application ? (args, asUser, roles) => client.runFlow(application.id, `${module}.${flow.key}`, args, asUser, roles) : undefined}
          runnable={listFlows(model).some((f) => f.module === module && f.flow.key === flow.key)}
          onSave={application && (user || offlineStart) && !readOnly ? async (f) => {
            const next = withFlow(model, module, f)
            const send = () => client.createProposal(application.id, next, `Edit flow ${f.key}`)
            const r = off ? await off.submit({ kind: 'proposal', path: `/applications/${application.id}/proposals`, body: { model: next, title: `Edit flow ${f.key}` }, label: `Edit flow ${f.key}` }, send) : { queued: false as const, value: await send() }
            if (r.queued) return 'Saved on this device. The proposal is created when you are online (and signed in); it is checked against the then-current model like any other.'
            const p = r.value
            return `Proposal created (${p.approval_level}, ${p.diff.changes.length} change${p.diff.changes.length === 1 ? '' : 's'}). It takes effect when someone with the right role approves it.`
          } : undefined}
        />
      </div>
    )
  }

  return (
    <>
    <PwaBar />
    {overlay}
    {!user && offlineStart ? (
      <div hidden={overlay !== null}>
        <OfflineGate
          onBoard={(b) => setBoard({ project: b.project, application: b.application, elements: [], canEdit: b.canEdit })}
          onOpenFlow={(o) => setOpen({ model: o.model, module: o.module, flow: o.flow, application: o.application, readOnly: false, demo: false })}>
          <section aria-label="Sign in">
            <h2>Back online?</h2>
            <p className="hint">Sign in to send what is waiting and to work with the live data.</p>
            <Login client={client} onUser={(u) => { setUser(u); setOfflineStart(false) }} />
          </section>
        </OfflineGate>
      </div>
    ) : (
    <main className="page" hidden={overlay !== null}>
      <h1>Orchivio Studio · Flow builder</h1>
      {user && <OfflinePanel />}
      {user ? (
        <Browser client={client} user={user} onOpen={setOpen} onBoard={setBoard} onPreview={setPreview} onAdmin={setAdmin}
                 onLogout={signOut} />
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
    )}
    </>
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

function Browser({ client, user, onOpen, onBoard, onPreview, onAdmin, onLogout }: { client: ApiClient; user: User; onOpen: (o: Open) => void; onBoard: (b: BoardTarget) => void; onPreview: (p: PreviewTarget) => void; onAdmin: (a: AdminTarget) => void; onLogout: () => void }) {
  const orgs = useLoad(() => client.organizations(), [client])
  const [org, setOrg] = useState<Org | null>(null)
  const projects = useLoad(() => (org ? client.projects(org.id) : Promise.resolve(null)), [org])
  const [project, setProject] = useState<Project | null>(null)
  const apps = useLoad(() => (project ? client.applications(project.id) : Promise.resolve(null)), [project])
  const [app, setApp] = useState<Application | null>(null)
  const revision = useLoad(() => (app?.current_revision_id ? client.revision(app.current_revision_id) : Promise.resolve(null)), [app])
  const [newKey, setNewKey] = useState('')
  const [error, setError] = useState('')
  const off = useOffline()
  useEffect(() => { if (app && revision.data) void saveModel(off, app.id, { name: app.name, model: revision.data.model_json }) }, [app, revision.data, off?.unlocked]) // eslint-disable-line react-hooks/exhaustive-deps

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
      {org && project && (
        <p>
          <button type="button" onClick={() => onAdmin({ org, project, application: app ?? undefined, model: revision.data?.model_json ?? null })}>Open administration</button>
        </p>
      )}
      {project && (
        <p>
          <button type="button" onClick={() => {
            onBoard({ project, application: app ?? undefined, canEdit, elements: revision.data ? collectElements(revision.data.model_json) : [] })
            if (org) void remember(off, { projectId: project.id, projectName: project.name, organization: org.name, canEdit, applications: (apps.data?.items ?? []).map((a) => ({ id: a.id, name: a.name, hasModel: a.id === app?.id && !!revision.data })) })
          }}>
            Open the story board
          </button>
        </p>
      )}
      {app && revision.data && (
        <p>
          <button type="button" onClick={() => onPreview({ application: app, model: revision.data!.model_json, canEdit,
            roles: ((revision.data!.model_json.security as { roles?: string[] } | undefined)?.roles ?? []) })}>
            Open the preview
          </button>
        </p>
      )}
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
