import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Danger, Feedback, Field, Loading, Panel, Pill, useAction, useApi, when } from './kit'

interface Tool { name: string; description: string; enabled: boolean; level: string }
interface Server { id: string; name: string; url: string; secret_ref: string | null; status: string; discovered_at: string | null; tools: Tool[] }

export function McpServers({ client, orgId }: { client: ApiClient; orgId: string }) {
  const servers = useApi(() => client.request<{ items: Server[] }>('GET', `/organizations/${orgId}/mcp-servers`), [client, orgId])
  const { run, error, notice } = useAction(servers.reload)
  const [form, setForm] = useState({ name: '', url: '', secret_ref: '' })
  const base = (s: Server) => `/organizations/${orgId}/mcp-servers/${encodeURIComponent(s.name)}`

  return (
    <>
      <Panel title="External MCP servers">
        <p className="hint">Servers the AI Builder may consult for background (documentation, tickets, design sources). New tools start <strong>off</strong>; only read-only tools can be enabled, because the Builder cannot ask a person before a tool changes something. Tool output is treated as untrusted text.</p>
        <Loading loading={servers.loading} error={servers.error} />
        <Feedback error={error} notice={notice} />
        {servers.data?.items.length === 0 && <p className="hint">No servers registered.</p>}
        <ul className="list">
          {servers.data?.items.map((s) => (
            <li key={s.id} className="card slim">
              <div className="row"><strong>{s.name}</strong> <Pill value={s.status} /> <small className="hint">{s.secret_ref ? `token ${s.secret_ref}` : 'no token'} · discovered {when(s.discovered_at)}</small></div>
              <div className="row">
                <button type="button" onClick={() => void run(() => client.request('POST', `${base(s)}/discover`), 'Tools discovered; new ones are off.')}>Discover tools</button>
                <button type="button" onClick={() => void run(() => client.request('PATCH', base(s), { status: s.status === 'active' ? 'disabled' : 'active' }))}>{s.status === 'active' ? 'Disable' : 'Enable'}</button>
                <Danger label="Remove" onConfirm={() => run(() => client.request('DELETE', base(s)), `${s.name} removed.`)} />
              </div>
              {s.tools.length > 0 && (
                <table className="trace"><tbody>
                  {s.tools.map((t) => (
                    <tr key={t.name}>
                      <td><code>{t.name}</code><br /><small className="hint">{t.description}</small></td>
                      <td><label className="field check"><input type="checkbox" aria-label={`Enable ${t.name}`} checked={t.enabled}
                        onChange={(e) => void run(() => client.request('PUT', `${base(s)}/tools/${encodeURIComponent(t.name)}`, { enabled: e.target.checked, level: 'A0' }))} /><span>{t.enabled ? 'on' : 'off'}</span></label></td>
                    </tr>
                  ))}
                </tbody></table>
              )}
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="Register a server">
        <form onSubmit={(e) => { e.preventDefault(); void run(() => client.request('POST', `/organizations/${orgId}/mcp-servers`, { name: form.name.trim(), url: form.url.trim(), ...(form.secret_ref.trim() ? { secret_ref: form.secret_ref.trim() } : {}) }), 'Server registered. Discover its tools next.').then(() => setForm({ name: '', url: '', secret_ref: '' })) }}>
          <Field label="Name" hint="Lower-case letters, digits and -"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="URL" hint="https; internal addresses are refused"><input type="url" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} /></Field>
          <Field label="Token reference (optional)" hint="env:NAME or file:/path"><input value={form.secret_ref} onChange={(e) => setForm({ ...form, secret_ref: e.target.value })} /></Field>
          <button type="submit" className="primary" disabled={!form.name.trim() || !form.url.trim()}>Register</button>
        </form>
      </Panel>
    </>
  )
}
