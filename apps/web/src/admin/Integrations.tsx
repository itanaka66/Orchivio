import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Danger, Feedback, Field, Loading, Panel, Pill, useAction, useApi } from './kit'

interface Destination { name: string; base_url: string; auth: { type: string; header: string | null; secret_ref: string | null }; credential_available: boolean }

export function Integrations({ client, appId }: { client: ApiClient; appId: string }) {
  const dests = useApi(() => client.request<{ items: Destination[] }>('GET', `/applications/${appId}/destinations`), [client, appId])
  const { run, error, notice } = useAction(dests.reload)
  const blank = { name: '', base_url: '', type: 'none', header: '', secret_ref: '' }
  const [form, setForm] = useState(blank)
  const [spec, setSpec] = useState('')
  const [specKey, setSpecKey] = useState('')
  const [specDest, setSpecDest] = useState('')
  const [importMsg, setImportMsg] = useState('')
  const [importErr, setImportErr] = useState('')

  const save = () => run(() => client.request('PUT', `/applications/${appId}/destinations/${encodeURIComponent(form.name.trim())}`, {
    base_url: form.base_url.trim(),
    auth: form.type === 'none' ? { type: 'none' } : { type: form.type, secret_ref: form.secret_ref.trim(), ...(form.type === 'header' ? { header: form.header.trim() } : {}) },
  }), 'Destination saved.').then(() => setForm(blank))

  return (
    <>
      <Panel title="Destinations">
        <p className="hint">Where the model's logical integrations really point. Only administrators set these; they decide what the application can reach. Credentials are references to secrets on the server.</p>
        <Loading loading={dests.loading} error={dests.error} />
        <Feedback error={error} notice={notice} />
        {dests.data?.items.length === 0 && <p className="hint">No destinations.</p>}
        <table className="trace"><tbody>
          {dests.data?.items.map((d) => (
            <tr key={d.name}>
              <td><strong>{d.name}</strong></td><td>{d.base_url}</td>
              <td>{d.auth.type === 'none' ? 'no auth' : `${d.auth.type}${d.auth.header ? ` (${d.auth.header})` : ''} · ${d.auth.secret_ref}`}</td>
              <td>{d.auth.type !== 'none' && (d.credential_available ? <Pill value="ok" /> : <span className="sev-error">credential missing on the server</span>)}</td>
              <td>
                <button type="button" className="link" onClick={() => setForm({ name: d.name, base_url: d.base_url, type: d.auth.type, header: d.auth.header ?? '', secret_ref: d.auth.secret_ref ?? '' })}>Edit</button>
                <Danger label="Delete" onConfirm={() => run(() => client.request('DELETE', `/applications/${appId}/destinations/${encodeURIComponent(d.name)}`), `${d.name} removed.`)} />
              </td>
            </tr>
          ))}
        </tbody></table>
        <form onSubmit={(e) => { e.preventDefault(); void save() }}>
          <h3>Add or replace a destination</h3>
          <Field label="Name" hint="Lower-case letters, digits and -"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Base URL" hint="https; no query string"><input type="url" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></Field>
          <Field label="Authentication"><select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
            <option value="none">None</option><option value="bearer">Bearer token</option><option value="header">API key header</option></select></Field>
          {form.type === 'header' && <Field label="Header name"><input value={form.header} onChange={(e) => setForm({ ...form, header: e.target.value })} placeholder="X-Api-Key" /></Field>}
          {form.type !== 'none' && <Field label="Secret reference" hint="env:NAME or file:/path"><input value={form.secret_ref} onChange={(e) => setForm({ ...form, secret_ref: e.target.value })} /></Field>}
          <button type="submit" className="primary" disabled={!form.name.trim() || !form.base_url.trim() || (form.type !== 'none' && !form.secret_ref.trim())}>Save destination</button>
        </form>
      </Panel>

      <Panel title="Import an OpenAPI description">
        <p className="hint">Turns an API description into a proposal that adds the integration to the model. Nothing changes until the proposal is approved.</p>
        <form onSubmit={async (e) => {
          e.preventDefault()
          setImportErr(''); setImportMsg('')
          try {
            const r = await client.requestText<{ id: string; title?: string }>('POST', `/applications/${appId}/integrations/openapi-import?key=${encodeURIComponent(specKey.trim())}&destination=${encodeURIComponent(specDest.trim())}`, spec)
            setImportMsg(`Proposal created${r.title ? `: ${r.title}` : ''}. Review it under proposals.`)
            setSpec('')
          } catch (err) { setImportErr(err instanceof Error ? err.message : 'Import failed.') }
        }}>
          <Field label="Integration key" hint="Lower-case letters, digits and _ ; how flows refer to it"><input value={specKey} onChange={(e) => setSpecKey(e.target.value)} /></Field>
          <Field label="Destination" hint="The destination (above) this API is reached through">
            <select value={specDest} onChange={(e) => setSpecDest(e.target.value)}><option value="">Choose…</option>{dests.data?.items.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}</select></Field>
          <Field label="OpenAPI (JSON or YAML)"><textarea rows={8} value={spec} onChange={(e) => setSpec(e.target.value)} /></Field>
          <button type="submit" disabled={!specKey.trim() || !specDest || !spec.trim()}>Create proposal</button>
        </form>
        {importErr && <p role="alert" className="sev-error">{importErr}</p>}
        {importMsg && <p role="status" className="hint">{importMsg}</p>}
      </Panel>
    </>
  )
}
