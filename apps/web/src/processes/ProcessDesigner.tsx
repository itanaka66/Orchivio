import { useState } from 'react'
import type { ApiClient } from '../api/client'

type Json = Record<string, unknown>
type StepType = 'start' | 'user_task' | 'system_task' | 'call_process' | 'decision' | 'parallel_split' | 'parallel_join' | 'timer' | 'catch_message' | 'send_message' | 'end'
const STEP_TYPES: StepType[] = ['start', 'end', 'user_task', 'system_task', 'call_process', 'decision', 'parallel_split', 'parallel_join', 'timer', 'catch_message', 'send_message']
const FIELD_TYPES = ['string', 'integer', 'decimal', 'boolean'] as const
const NO_CONFIG_TYPES: StepType[] = ['start', 'end', 'parallel_split', 'parallel_join']

interface Param { name: string; type: typeof FIELD_TYPES[number] }
interface Step { id: string; type: StepType; label?: string; config: Record<string, unknown> }
interface Edge { id: string; source: string; target: string; outcome?: string }
interface ProcessDef {
  key: string; description?: string; start_roles: string[]; run_as_role?: string
  params: Param[]; steps: Step[]; edges: Edge[]
}
interface Module { key: string; processes?: ProcessDef[]; flows?: { key: string; kind?: string }[] }

function blankProcess(): ProcessDef {
  return { key: '', description: '', start_roles: [], params: [], steps: [{ id: 'start', type: 'start', config: {} }, { id: 'end', type: 'end', config: {} }], edges: [] }
}

function defaultConfig(t: StepType): Record<string, unknown> {
  if (t === 'decision') return { expr: '' }
  if (t === 'timer') return { wait_seconds: 3600 }
  if (t === 'user_task') return { title: '', assignee_role: '', outcomes: ['approve', 'reject'] }
  if (t === 'system_task') return { flow: '', run_as_role: '', args: {} }
  if (t === 'call_process') return { process: '', args: {} }
  if (t === 'catch_message') return { message: '' }
  if (t === 'send_message') return { message_type: '', payload_expr: {}, target: 'broadcast' }
  return {}
}

function withProcesses(model: Json, moduleKey: string, procs: ProcessDef[]): Json {
  return { ...model, modules: ((model.modules ?? []) as Module[]).map((m) => m.key === moduleKey ? { ...m, processes: procs } : m) }
}

function parseOutcomes(s: string): string[] { return s.split(',').map((x) => x.trim().toLowerCase()).filter(Boolean) }
function fmtOutcomes(outcomes: unknown): string {
  return Array.isArray(outcomes) ? outcomes.join(', ') : typeof outcomes === 'string' ? outcomes : ''
}
function fmtArgs(args: unknown): string { try { return JSON.stringify(args ?? {}) } catch { return '{}' } }
function parseArgs(s: string): Record<string, string> { try { const v = JSON.parse(s || '{}'); return typeof v === 'object' && v && !Array.isArray(v) ? v as Record<string, string> : {} } catch { return {} } }

interface Props { client: ApiClient; applicationId: string; model: Json; canEdit: boolean }

export function ProcessDesigner({ client, applicationId, model, canEdit }: Props) {
  const modules = (model.modules ?? []) as Module[]
  const mod = modules[0]
  const roles = ((model.security as { roles?: string[] } | undefined)?.roles ?? [])
  const serverFlows = (mod?.flows ?? []).filter((f) => !f.kind || f.kind === 'server').map((f) => f.key)
  const processes: ProcessDef[] = (mod?.processes ?? []) as ProcessDef[]

  const [draft, setDraft] = useState<ProcessDef | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [stepDraft, setStepDraft] = useState<Step | null>(null)
  const [isNewStep, setIsNewStep] = useState(false)
  const [edgeDraft, setEdgeDraft] = useState<Edge | null>(null)
  const [isNewEdge, setIsNewEdge] = useState(false)
  const [paramDraft, setParamDraft] = useState<Param | null>(null)
  const [isNewParam, setIsNewParam] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  if (!mod) return <p className="hint">No modules found in this model.</p>

  const submit = async (procs: ProcessDef[], title: string) => {
    setBusy(true); setError('')
    try { await client.createProposal(applicationId, withProcesses(model, mod.key, procs), title); setNotice(`Proposal submitted: "${title}"`); setDraft(null) }
    catch (e) { setError(e instanceof Error ? e.message : 'Failed.') }
    finally { setBusy(false) }
  }

  const saveStep = () => {
    if (!draft || !stepDraft) return
    if (!stepDraft.id.trim()) { setError('Step ID is required.'); return }
    const others = isNewStep ? draft.steps : draft.steps.filter((s) => s.id !== stepDraft.id)
    if (isNewStep && draft.steps.some((s) => s.id === stepDraft.id)) { setError(`Step "${stepDraft.id}" already exists.`); return }
    setDraft({ ...draft, steps: isNewStep ? [...draft.steps, stepDraft] : draft.steps.map((s) => s.id === stepDraft.id ? stepDraft : s) })
    setStepDraft(null); setError('')
    void others // suppress lint
  }

  const saveEdge = () => {
    if (!draft || !edgeDraft) return
    if (!edgeDraft.id.trim() || !edgeDraft.source || !edgeDraft.target) { setError('Edge needs an ID, source and target.'); return }
    if (isNewEdge && draft.edges.some((e) => e.id === edgeDraft.id)) { setError(`Edge "${edgeDraft.id}" already exists.`); return }
    setDraft({ ...draft, edges: isNewEdge ? [...draft.edges, edgeDraft] : draft.edges.map((e) => e.id === edgeDraft.id ? edgeDraft : e) })
    setEdgeDraft(null); setError('')
  }

  const removeStep = (id: string) => draft && setDraft({ ...draft, steps: draft.steps.filter((s) => s.id !== id), edges: draft.edges.filter((e) => e.source !== id && e.target !== id) })
  const removeEdge = (id: string) => draft && setDraft({ ...draft, edges: draft.edges.filter((e) => e.id !== id) })

  const saveParam = () => {
    if (!draft || !paramDraft) return
    if (!paramDraft.name.trim()) { setError('Parameter name is required.'); return }
    if (isNewParam && draft.params.some((p) => p.name === paramDraft.name)) { setError(`Parameter "${paramDraft.name}" already exists.`); return }
    setDraft({ ...draft, params: isNewParam ? [...draft.params, paramDraft] : draft.params.map((p) => p.name === paramDraft.name ? paramDraft : p) })
    setParamDraft(null); setError('')
  }
  const removeParam = (name: string) => draft && setDraft({ ...draft, params: draft.params.filter((p) => p.name !== name) })

  const submitDraft = () => {
    if (!draft) return
    if (!draft.key.trim()) { setError('Process key is required.'); return }
    if (isNew && processes.some((p) => p.key === draft.key)) { setError(`Process "${draft.key}" already exists.`); return }
    const procs = isNew ? [...processes, draft] : processes.map((p) => p.key === draft.key ? draft : p)
    void submit(procs, `${isNew ? 'Add' : 'Edit'} process ${draft.key}`)
  }

  const stepIds = draft?.steps.map((s) => s.id) ?? []

  return (
    <div className="page-designer">
      <div className="designer-list card">
        <div className="panel-head"><h2>Processes</h2></div>
        {processes.length === 0 && <p className="hint">No processes yet.</p>}
        <ul className="list">
          {processes.map((p) => (
            <li key={p.key}>
              <button type="button" className="link" aria-current={draft?.key === p.key && !isNew ? true : undefined}
                onClick={() => { setDraft({ ...p }); setIsNew(false); setStepDraft(null); setEdgeDraft(null); setNotice(''); setError('') }}>
                {p.key}<small> · {p.steps.length} steps</small>
              </button>
              {canEdit && (
                <button type="button" className="link" aria-label={`Delete process ${p.key}`}
                  onClick={() => { if (window.confirm(`Delete process "${p.key}"? A proposal will be submitted.`)) void submit(processes.filter((x) => x.key !== p.key), `Delete process ${p.key}`) }}>Delete</button>
              )}
            </li>
          ))}
        </ul>
        {canEdit && <p><button type="button" className="primary" onClick={() => { setDraft(blankProcess()); setIsNew(true); setStepDraft(null); setEdgeDraft(null); setNotice(''); setError('') }}>New process</button></p>}
      </div>

      <div className="designer-edit">
        {error && <p role="alert" className="sev-error">{error}</p>}
        {notice && <p role="status" className="hint">{notice}</p>}
        {!draft && <p className="hint">Select a process to edit it, or create a new one.</p>}
        {draft && (
          <div>
            {/* Metadata */}
            <section className="card" aria-label={isNew ? 'New process' : `Edit process ${draft.key}`}>
              <div className="panel-head"><h2>{isNew ? 'New process' : `Edit ${draft.key}`}</h2></div>
              <div className="form-grid">
                {isNew && <label className="field"><span>Key</span><input required value={draft.key} placeholder="Approval" onChange={(e) => setDraft({ ...draft, key: e.target.value })} /></label>}
                <label className="field"><span>Description</span><input value={draft.description ?? ''} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
                <label className="field"><span>Start roles</span>
                  <input value={draft.start_roles.join(', ')} placeholder="viewer, editor"
                    onChange={(e) => setDraft({ ...draft, start_roles: e.target.value.split(',').map((r) => r.trim()).filter(Boolean) })} /></label>
                <label className="field"><span>Run-as role</span>
                  <select value={draft.run_as_role ?? ''} onChange={(e) => setDraft({ ...draft, run_as_role: e.target.value || undefined })}>
                    <option value="">— none —</option>
                    {roles.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select></label>
              </div>
            </section>

            {/* Params */}
            <section className="card">
              <div className="panel-head"><h2>Parameters</h2></div>
              <p className="hint">Values the caller must provide when starting this process.</p>
              {draft.params.length > 0 && (
                <table className="trace">
                  <thead><tr><th>Name</th><th>Type</th><th /></tr></thead>
                  <tbody>
                    {draft.params.map((p) => (
                      <tr key={p.name}>
                        <td><code>{p.name}</code></td><td>{p.type}</td>
                        <td>
                          {canEdit && <><button type="button" className="link" onClick={() => { setParamDraft({ ...p }); setIsNewParam(false) }}>Edit</button>{' '}
                          <button type="button" className="link" aria-label={`Remove parameter ${p.name}`} onClick={() => removeParam(p.name)}>Remove</button></>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {draft.params.length === 0 && <p className="hint">No parameters.</p>}
              {canEdit && <p><button type="button" onClick={() => { setParamDraft({ name: '', type: 'string' }); setIsNewParam(true) }}>Add parameter</button></p>}

              {paramDraft && (
                <form className="card" aria-label={isNewParam ? 'Add parameter' : `Edit parameter ${paramDraft.name}`}
                  onSubmit={(e) => { e.preventDefault(); saveParam() }}>
                  <div className="panel-head"><h3>{isNewParam ? 'Add parameter' : `Edit ${paramDraft.name}`}</h3></div>
                  <label className="field"><span>Name</span><input required value={paramDraft.name} placeholder="amount" onChange={(e) => setParamDraft({ ...paramDraft, name: e.target.value })} /></label>
                  <label className="field"><span>Type</span>
                    <select value={paramDraft.type} onChange={(e) => setParamDraft({ ...paramDraft, type: e.target.value as Param['type'] })}>
                      {FIELD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select></label>
                  <div className="toolbar">
                    <button type="submit" className="primary">{isNewParam ? 'Add' : 'Update'}</button>
                    <button type="button" onClick={() => { setParamDraft(null); setError('') }}>Cancel</button>
                  </div>
                </form>
              )}
            </section>

            {/* Steps */}
            <section className="card">
              <div className="panel-head"><h2>Steps</h2></div>
              <table className="trace">
                <thead><tr><th>ID</th><th>Type</th><th>Label</th><th /></tr></thead>
                <tbody>
                  {draft.steps.map((s) => (
                    <tr key={s.id}>
                      <td><code>{s.id}</code></td><td><span className="badge">{s.type}</span></td><td>{s.label ?? ''}</td>
                      <td>
                        {canEdit && <><button type="button" className="link" onClick={() => { setStepDraft({ ...s }); setIsNewStep(false) }}>Edit</button>{' '}
                        <button type="button" className="link" aria-label={`Remove step ${s.id}`} onClick={() => removeStep(s.id)}>Remove</button></>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {canEdit && <p><button type="button" onClick={() => { setStepDraft({ id: '', type: 'user_task', config: defaultConfig('user_task') }); setIsNewStep(true) }}>Add step</button></p>}

              {stepDraft && (
                <form className="card" aria-label={isNewStep ? 'Add step' : `Edit step ${stepDraft.id}`}
                  onSubmit={(e) => { e.preventDefault(); saveStep() }}>
                  <div className="panel-head"><h3>{isNewStep ? 'Add step' : `Edit ${stepDraft.id}`}</h3></div>
                  <label className="field"><span>ID</span><input required value={stepDraft.id} onChange={(e) => setStepDraft({ ...stepDraft, id: e.target.value })} /></label>
                  <label className="field"><span>Type</span>
                    <select value={stepDraft.type} onChange={(e) => { const t = e.target.value as StepType; setStepDraft({ ...stepDraft, type: t, config: defaultConfig(t) }) }}>
                      {STEP_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select></label>
                  <label className="field"><span>Label (optional)</span><input value={stepDraft.label ?? ''} onChange={(e) => setStepDraft({ ...stepDraft, label: e.target.value || undefined })} /></label>
                  {!NO_CONFIG_TYPES.includes(stepDraft.type) && <StepConfig step={stepDraft} roles={roles} flows={serverFlows} onChange={(cfg) => setStepDraft({ ...stepDraft, config: cfg })} />}
                  <div className="toolbar">
                    <button type="submit" className="primary">{isNewStep ? 'Add' : 'Update'}</button>
                    <button type="button" onClick={() => { setStepDraft(null); setError('') }}>Cancel</button>
                  </div>
                </form>
              )}
            </section>

            {/* Edges */}
            <section className="card">
              <div className="panel-head"><h2>Edges</h2></div>
              <table className="trace">
                <thead><tr><th>ID</th><th>Source</th><th>Target</th><th>Outcome</th><th /></tr></thead>
                <tbody>
                  {draft.edges.map((e) => (
                    <tr key={e.id}>
                      <td><code>{e.id}</code></td><td><code>{e.source}</code></td><td><code>{e.target}</code></td><td>{e.outcome ?? ''}</td>
                      <td>
                        {canEdit && <><button type="button" className="link" onClick={() => { setEdgeDraft({ ...e }); setIsNewEdge(false) }}>Edit</button>{' '}
                        <button type="button" className="link" aria-label={`Remove edge ${e.id}`} onClick={() => removeEdge(e.id)}>Remove</button></>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {canEdit && <p><button type="button" onClick={() => { setEdgeDraft({ id: '', source: stepIds[0] ?? '', target: stepIds[1] ?? '' }); setIsNewEdge(true) }}>Add edge</button></p>}

              {edgeDraft && (
                <form className="card" aria-label={isNewEdge ? 'Add edge' : `Edit edge ${edgeDraft.id}`}
                  onSubmit={(e) => { e.preventDefault(); saveEdge() }}>
                  <div className="panel-head"><h3>{isNewEdge ? 'Add edge' : `Edit ${edgeDraft.id}`}</h3></div>
                  <label className="field"><span>ID</span><input required value={edgeDraft.id} onChange={(e) => setEdgeDraft({ ...edgeDraft, id: e.target.value })} /></label>
                  <label className="field"><span>Source</span>
                    <select value={edgeDraft.source} onChange={(e) => setEdgeDraft({ ...edgeDraft, source: e.target.value })}>
                      {stepIds.map((id) => <option key={id} value={id}>{id}</option>)}
                    </select></label>
                  <label className="field"><span>Target</span>
                    <select value={edgeDraft.target} onChange={(e) => setEdgeDraft({ ...edgeDraft, target: e.target.value })}>
                      {stepIds.map((id) => <option key={id} value={id}>{id}</option>)}
                    </select></label>
                  <label className="field"><span>Outcome (optional)</span><input value={edgeDraft.outcome ?? ''} placeholder="approve" onChange={(e) => setEdgeDraft({ ...edgeDraft, outcome: e.target.value || undefined })} /></label>
                  <div className="toolbar">
                    <button type="submit" className="primary">{isNewEdge ? 'Add' : 'Update'}</button>
                    <button type="button" onClick={() => { setEdgeDraft(null); setError('') }}>Cancel</button>
                  </div>
                </form>
              )}
            </section>

            {canEdit && (
              <p>
                <button type="button" className="primary" disabled={busy} onClick={submitDraft}>{busy ? 'Submitting…' : 'Submit as proposal'}</button>
                <button type="button" onClick={() => { setDraft(null); setStepDraft(null); setEdgeDraft(null); setError('') }}> Cancel</button>
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function StepConfig({ step, roles, flows, onChange }: { step: Step; roles: string[]; flows: string[]; onChange: (c: Record<string, unknown>) => void }) {
  const c = step.config

  if (step.type === 'decision') return (
    <label className="field"><span>Expression</span>
      <input value={String(c.expr ?? '')} placeholder="amount > 1000"
        onChange={(e) => onChange({ ...c, expr: e.target.value })} /></label>
  )

  if (step.type === 'timer') return (
    <label className="field"><span>Wait seconds</span>
      <input type="number" min={1} value={Number(c.wait_seconds ?? 3600)}
        onChange={(e) => onChange({ ...c, wait_seconds: Number(e.target.value) })} /></label>
  )

  if (step.type === 'catch_message') return (
    <label className="field"><span>Message name</span>
      <input value={String(c.message ?? '')} placeholder="request_approved"
        onChange={(e) => onChange({ ...c, message: e.target.value })} /></label>
  )

  if (step.type === 'call_process') return (
    <>
      <label className="field"><span>Process key</span>
        <input required value={String(c.process ?? '')} placeholder="Approval" onChange={(e) => onChange({ ...c, process: e.target.value })} /></label>
      <label className="field"><span>Args (JSON object)</span>
        <input value={fmtArgs(c.args)} placeholder='{"amount": "data.amount"}'
          onChange={(e) => onChange({ ...c, args: parseArgs(e.target.value) })} /></label>
    </>
  )

  if (step.type === 'send_message') return (
    <>
      <label className="field"><span>Message type</span>
        <input required value={String(c.message_type ?? '')} placeholder="request_approved"
          onChange={(e) => onChange({ ...c, message_type: e.target.value })} /></label>
      <label className="field"><span>Target</span>
        <input value={String(c.target ?? 'broadcast')} placeholder="broadcast"
          onChange={(e) => onChange({ ...c, target: e.target.value || 'broadcast' })} /></label>
      <label className="field"><span>Payload expressions (JSON object)</span>
        <input value={fmtArgs(c.payload_expr)} placeholder='{"ref": "data.id"}'
          onChange={(e) => onChange({ ...c, payload_expr: parseArgs(e.target.value) })} /></label>
    </>
  )

  if (step.type === 'system_task') return (
    <>
      <label className="field"><span>Flow key</span>
        {flows.length > 0
          ? <select value={String(c.flow ?? '')} onChange={(e) => onChange({ ...c, flow: e.target.value })}>
              <option value="">— select flow —</option>
              {flows.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          : <input value={String(c.flow ?? '')} placeholder="ReorderLowStock" onChange={(e) => onChange({ ...c, flow: e.target.value })} />}
      </label>
      <label className="field"><span>Run-as role</span>
        <select value={String(c.run_as_role ?? '')} onChange={(e) => onChange({ ...c, run_as_role: e.target.value })}>
          <option value="">— select role —</option>
          {roles.map((r) => <option key={r} value={r}>{r}</option>)}
        </select></label>
      <label className="field"><span>Args (JSON object)</span>
        <input value={fmtArgs(c.args)} placeholder='{"min_gap": "0"}'
          onChange={(e) => onChange({ ...c, args: parseArgs(e.target.value) })} /></label>
    </>
  )

  if (step.type === 'user_task') {
    const useExpr = !!c.assignee_expr
    return (
      <>
        <label className="field"><span>Title</span>
          <input required value={String(c.title ?? '')} onChange={(e) => onChange({ ...c, title: e.target.value })} /></label>
        <fieldset>
          <legend>Assignee</legend>
          <label className="checkbox"><input type="radio" name={`assignee-${step.id}`} checked={!useExpr}
            onChange={() => onChange({ ...c, assignee_role: c.assignee_role ?? '', assignee_expr: undefined })} /> Role</label>
          <label className="checkbox"><input type="radio" name={`assignee-${step.id}`} checked={useExpr}
            onChange={() => onChange({ ...c, assignee_expr: c.assignee_expr ?? 'initiator', assignee_role: undefined })} /> Expression</label>
          {!useExpr
            ? <select value={String(c.assignee_role ?? '')} onChange={(e) => onChange({ ...c, assignee_role: e.target.value })}>
                <option value="">— select role —</option>
                {roles.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            : <input value={String(c.assignee_expr ?? '')} placeholder="initiator"
                onChange={(e) => onChange({ ...c, assignee_expr: e.target.value })} />}
        </fieldset>
        <label className="field"><span>Outcomes (comma-separated)</span>
          <input value={fmtOutcomes(c.outcomes)} placeholder="approve, reject"
            onChange={(e) => onChange({ ...c, outcomes: parseOutcomes(e.target.value) })} /></label>
        <label className="field"><span>Due in hours (optional)</span>
          <input type="number" min={1} value={c.due_in_hours != null ? Number(c.due_in_hours) : ''}
            onChange={(e) => onChange({ ...c, due_in_hours: e.target.value ? Number(e.target.value) : undefined })} /></label>
        <label className="field"><span>Escalate after hours (optional)</span>
          <input type="number" min={1} value={c.escalate_after_hours != null ? Number(c.escalate_after_hours) : ''}
            onChange={(e) => onChange({ ...c, escalate_after_hours: e.target.value ? Number(e.target.value) : undefined })} /></label>
        <label className="field"><span>Escalate to role (optional)</span>
          <select value={String(c.escalate_to_role ?? '')} onChange={(e) => onChange({ ...c, escalate_to_role: e.target.value || undefined })}>
            <option value="">— none —</option>
            {roles.map((r) => <option key={r} value={r}>{r}</option>)}
          </select></label>
      </>
    )
  }

  return null
}
