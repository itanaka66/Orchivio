import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { ApiError } from '../api/client'
import { Danger, Feedback, Field, Loading, Panel, Pill, useAction, useApi } from './kit'

interface Pack { id: string; name: string; description: string | null; status: string; rules: Record<string, unknown>[] }
interface Compliance { packs: string[]; summary: Record<string, number>; applications: { application_id: string; project: string; application: string; status: string; violations: { rule: string; severity: string; message: string; path?: string; pack: string }[] }[] }
interface Inventory { applications?: { application_id: string; project: string; application: string; revision_no: number | null; entities: number; flows: number; app_users: number; risks: string[] }[]; items?: Inventory['applications'] }
interface Usage { totals: { jobs?: number; input_tokens?: number; output_tokens?: number; cost_usd: number }; mcp_tool_calls: number; by_project: Record<string, { jobs?: number; cost_usd: number }>; by_type: Record<string, { jobs?: number; cost_usd: number }> }

type Tab = 'packs' | 'compliance' | 'inventory' | 'ai' | 'audit'
const TABS: [Tab, string][] = [['packs', 'Policy packs'], ['compliance', 'Compliance'], ['inventory', 'Inventory'], ['ai', 'AI usage'], ['audit', 'Audit export']]

export function Governance({ client, orgId }: { client: ApiClient; orgId: string }) {
  const [tab, setTab] = useState<Tab>('packs')
  return (
    <>
      <div className="tabs" role="tablist" aria-label="Governance sections">
        {TABS.map(([t, label]) => <button key={t} type="button" role="tab" aria-selected={tab === t} className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>{label}</button>)}
      </div>
      {tab === 'packs' && <Packs client={client} orgId={orgId} />}
      {tab === 'compliance' && <ComplianceView client={client} orgId={orgId} />}
      {tab === 'inventory' && <InventoryView client={client} orgId={orgId} />}
      {tab === 'ai' && <AiUsage client={client} orgId={orgId} />}
      {tab === 'audit' && <AuditExport client={client} orgId={orgId} />}
    </>
  )
}

function Packs({ client, orgId }: { client: ApiClient; orgId: string }) {
  const packs = useApi(() => client.request<{ items: Pack[] }>('GET', `/organizations/${orgId}/policy-packs`), [client, orgId])
  const catalogue = useApi(() => client.request<{ rules: { rule: string; description: string }[] }>('GET', '/governance/policy-rules'), [client])
  const { run, error, notice } = useAction(packs.reload)
  const [edit, setEdit] = useState<{ id?: string; name: string; description: string; rules: string } | null>(null)
  const [rulesError, setRulesError] = useState('')
  const [details, setDetails] = useState<unknown>(null)

  const save = async () => {
    if (!edit) return
    let rules: unknown
    try { rules = JSON.parse(edit.rules) } catch { setRulesError('The rules must be valid JSON: a list of {"rule": …} objects.'); return }
    if (!Array.isArray(rules)) { setRulesError('The rules must be a JSON list.'); return }
    setRulesError(''); setDetails(null)
    try {
      if (edit.id) await client.request('PATCH', `/organizations/${orgId}/policy-packs/${edit.id}`, { description: edit.description || null, rules })
      else await client.request('POST', `/organizations/${orgId}/policy-packs`, { name: edit.name.trim(), description: edit.description || null, rules })
      setEdit(null)
      packs.reload()
    } catch (e) {
      setRulesError(e instanceof Error ? e.message : 'Could not save.')
      if (e instanceof ApiError && e.details) setDetails(e.details)
    }
  }

  return (
    <>
      <Panel title="Policy packs" actions={<button type="button" className="primary" onClick={() => { setRulesError(''); setDetails(null); setEdit({ name: '', description: '', rules: '[\n  {"rule": "no-anonymous-access", "severity": "error"}\n]' }) }}>New pack</button>}>
        <p className="hint">Rules every change to every application of the organization has to meet. Errors block the change; warnings are reported. Existing revisions are never rewritten.</p>
        <Loading loading={packs.loading} error={packs.error} />
        <Feedback error={error} notice={notice} />
        {packs.data?.items.length === 0 && <p className="hint">No packs.</p>}
        <ul className="list">
          {packs.data?.items.map((p) => (
            <li key={p.id} className="card slim">
              <div className="row"><strong>{p.name}</strong> <Pill value={p.status} /> <small className="hint">{p.rules.map((r) => String(r.rule)).join(', ')}</small></div>
              {p.description && <p className="hint">{p.description}</p>}
              <div className="row">
                <button type="button" onClick={() => { setRulesError(''); setDetails(null); setEdit({ id: p.id, name: p.name, description: p.description ?? '', rules: JSON.stringify(p.rules, null, 2) }) }}>Edit</button>
                <button type="button" onClick={() => void run(() => client.request('PATCH', `/organizations/${orgId}/policy-packs/${p.id}`, { status: p.status === 'active' ? 'disabled' : 'active' }))}>{p.status === 'active' ? 'Disable' : 'Enable'}</button>
                <Danger label="Delete" onConfirm={() => run(() => client.request('DELETE', `/organizations/${orgId}/policy-packs/${p.id}`), `${p.name} deleted.`)} />
              </div>
            </li>
          ))}
        </ul>
      </Panel>
      {edit && (
        <Panel title={edit.id ? `Edit ${edit.name}` : 'New policy pack'}>
          {!edit.id && <Field label="Name"><input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></Field>}
          <Field label="Description"><input value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} /></Field>
          <Field label="Rules (JSON)"><textarea rows={8} className="mono" value={edit.rules} onChange={(e) => setEdit({ ...edit, rules: e.target.value })} /></Field>
          <details><summary>Available rules</summary><ul>{catalogue.data?.rules.map((r) => <li key={r.rule}><code>{r.rule}</code> — {r.description}</li>)}</ul></details>
          {rulesError && <p role="alert" className="sev-error">{rulesError}</p>}
          {details !== null && <pre className="json">{JSON.stringify(details, null, 2)}</pre>}
          <div className="toolbar"><button type="button" className="primary" disabled={!edit.id && !edit.name.trim()} onClick={() => void save()}>Save</button><button type="button" onClick={() => setEdit(null)}>Cancel</button></div>
        </Panel>
      )}
    </>
  )
}

function ComplianceView({ client, orgId }: { client: ApiClient; orgId: string }) {
  const c = useApi(() => client.request<Compliance>('GET', `/organizations/${orgId}/governance/compliance`), [client, orgId])
  const { run, error, notice } = useAction(c.reload)
  const [proposalNotice, setProposalNotice] = useState<Record<string, string>>({})

  const createProposal = async (appId: string, violation: Compliance['applications'][0]['violations'][0]) => {
    const key = `${appId}:${violation.rule}`
    try {
      await client.request('POST', `/applications/${appId}/proposals`, {
        title: `Fix ${violation.rule} violation`,
        description: `Policy pack "${violation.pack}" reports: ${violation.message}${violation.path ? ` at ${violation.path}` : ''}`,
        type: 'compliance_fix',
      })
      setProposalNotice((p) => ({ ...p, [key]: 'Proposal created.' }))
    } catch (e) {
      setProposalNotice((p) => ({ ...p, [key]: e instanceof Error ? e.message : 'Failed.' }))
    }
  }

  return (
    <Panel title="Compliance">
      <Loading loading={c.loading} error={c.error} />
      <Feedback error={error} notice={notice} />
      {c.data && (
        <>
          <p>{c.data.packs.length === 0 ? 'No active packs.' : `Against: ${c.data.packs.join(', ')}`}</p>
          <p>{Object.entries(c.data.summary).map(([k, v]) => <span key={k} className="chip">{k}: {v}</span>)}</p>
          <ul className="list">
            {c.data.applications.map((a) => (
              <li key={a.application_id} className="card slim">
                <strong>{a.project}/{a.application}</strong> <Pill value={a.status} />
                <ul>
                  {a.violations.map((v, i) => {
                    const key = `${a.application_id}:${v.rule}`
                    return (
                      <li key={i} className={v.severity === 'error' ? 'sev-error' : 'sev-warning'} style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                        <span>{v.pack}: {v.message}{v.path ? ` (${v.path})` : ''}</span>
                        {/* FR-124: create compliance fix proposal */}
                        <button type="button" className="link" style={{ fontSize: '0.8rem', flexShrink: 0 }}
                          onClick={() => void run(() => createProposal(a.application_id, v), '')}>
                          Create proposal
                        </button>
                        {proposalNotice[key] && <small className="hint">{proposalNotice[key]}</small>}
                      </li>
                    )
                  })}
                </ul>
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  )
}

function InventoryView({ client, orgId }: { client: ApiClient; orgId: string }) {
  const inv = useApi(() => client.request<Inventory>('GET', `/organizations/${orgId}/governance/inventory`), [client, orgId])
  const rows = inv.data?.applications ?? inv.data?.items ?? []
  return (
    <Panel title="Application inventory">
      <Loading loading={inv.loading} error={inv.error} />
      <table className="trace">
        <thead><tr><th>Application</th><th>Revision</th><th>Entities</th><th>Flows</th><th>Users</th><th>Risks</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.application_id}><td>{r.project}/{r.application}</td><td>{r.revision_no ?? '—'}</td><td>{r.entities}</td><td>{r.flows}</td><td>{r.app_users}</td>
              <td>{r.risks.map((k) => <span key={k} className="chip sev-warning">{k}</span>)}</td></tr>
          ))}
        </tbody>
      </table>
    </Panel>
  )
}

function AiUsage({ client, orgId }: { client: ApiClient; orgId: string }) {
  const u = useApi(() => client.request<Usage>('GET', `/organizations/${orgId}/governance/ai-usage`), [client, orgId])
  const money = (n: number) => `$${n.toFixed(4)}`
  return (
    <Panel title="AI usage (last 30 days)">
      <Loading loading={u.loading} error={u.error} />
      {u.data && (
        <>
          <dl className="coverage">
            <div><dt>Jobs</dt><dd>{u.data.totals.jobs ?? 0}</dd></div>
            <div><dt>Tokens in / out</dt><dd>{u.data.totals.input_tokens ?? 0} / {u.data.totals.output_tokens ?? 0}</dd></div>
            <div><dt>Cost</dt><dd>{money(u.data.totals.cost_usd)}</dd></div>
            <div><dt>MCP tool calls</dt><dd>{u.data.mcp_tool_calls}</dd></div>
          </dl>
          <h3>By project</h3>
          <table className="trace"><tbody>{Object.entries(u.data.by_project).map(([k, v]) => <tr key={k}><td>{k}</td><td>{v.jobs ?? 0} jobs</td><td>{money(v.cost_usd)}</td></tr>)}</tbody></table>
          <h3>By job type</h3>
          <table className="trace"><tbody>{Object.entries(u.data.by_type).map(([k, v]) => <tr key={k}><td>{k}</td><td>{v.jobs ?? 0} jobs</td><td>{money(v.cost_usd)}</td></tr>)}</tbody></table>
        </>
      )}
    </Panel>
  )
}

function AuditExport({ client, orgId }: { client: ApiClient; orgId: string }) {
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [prefix, setPrefix] = useState('')
  const link = (format: 'jsonl' | 'csv') => {
    const q = new URLSearchParams({ format })
    if (from) q.set('from', from)
    if (to) q.set('to', to)
    if (prefix) q.set('action_prefix', prefix)
    return client.url(`/organizations/${orgId}/governance/audit-export?${q.toString()}`)
  }
  return (
    <Panel title="Audit export">
      <p className="hint">Downloads up to 50 000 events per request, oldest first, with a content hash in the response headers. Every export is itself audited.</p>
      <Field label="From (date or time)"><input value={from} onChange={(e) => setFrom(e.target.value)} placeholder="2026-01-01" /></Field>
      <Field label="To"><input value={to} onChange={(e) => setTo(e.target.value)} /></Field>
      <Field label="Action starts with"><input value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="deployment." /></Field>
      <p><a href={link('jsonl')} download>Download JSON lines</a> · <a href={link('csv')} download>Download CSV</a></p>
    </Panel>
  )
}
