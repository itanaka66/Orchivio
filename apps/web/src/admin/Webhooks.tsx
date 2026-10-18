import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Danger, Feedback, Field, Json, Loading, Pager, Panel, Pill, useAction, useApi, when, type Page } from './kit'

interface Webhook { id: string; name: string; url: string; secret_ref: string; events: string[]; fields: string[] | null; status: string; paused_reason: string | null; consecutive_failures: number }
interface Delivery { id: string; event_type: string; status: string; attempt: number; max_attempts: number; response_status: number | null; error: string | null; delivered_at: string | null; last_attempt_at: string | null }
const LIMIT = 15

/** "a, b" → ["a","b"] without empty entries. */
const list = (text: string): string[] => text.split(',').map((x) => x.trim()).filter(Boolean)

export function Webhooks({ client, appId }: { client: ApiClient; appId: string }) {
  const hooks = useApi(() => client.request<{ items: Webhook[] }>('GET', `/applications/${appId}/webhooks`), [client, appId])
  const { run, error, notice } = useAction(hooks.reload)
  const [form, setForm] = useState({ name: '', url: '', secret_ref: '', events: '', fields: '' })
  const [selected, setSelected] = useState<Webhook | null>(null)

  return (
    <>
      <Panel title="Outbound webhooks">
        <p className="hint">Signed HTTP calls to other systems when business events happen. Secrets are references (env:NAME or file:/path); URLs are shown here only to administrators.</p>
        <Loading loading={hooks.loading} error={hooks.error} />
        <Feedback error={error} notice={notice} />
        {hooks.data?.items.length === 0 && <p className="hint">No webhooks.</p>}
        <ul className="list">
          {hooks.data?.items.map((w) => (
            <li key={w.id} className="card slim">
              <div className="row"><strong>{w.name}</strong> <Pill value={w.status} />
                <small className="hint">{w.events.join(', ')} · {w.fields === null ? 'ids only' : w.fields.join(', ')}</small></div>
              {w.paused_reason && <p className="sev-warning">{w.paused_reason}{w.consecutive_failures ? ` (${w.consecutive_failures} failures in a row)` : ''}</p>}
              <div className="row">
                <button type="button" onClick={() => setSelected(selected?.id === w.id ? null : w)}>{selected?.id === w.id ? 'Hide deliveries' : 'Deliveries'}</button>
                <button type="button" onClick={() => void run(() => client.request('POST', `/webhooks/${w.id}/test`), 'Test delivery queued; watch the deliveries.')}>Send test</button>
                <button type="button" onClick={() => void run(() => client.request('PATCH', `/webhooks/${w.id}`, { status: w.status === 'active' ? 'paused' : 'active' }))}>{w.status === 'active' ? 'Pause' : 'Resume'}</button>
                <Danger label="Delete" onConfirm={() => run(() => client.request('DELETE', `/webhooks/${w.id}`), `${w.name} deleted.`)} />
              </div>
            </li>
          ))}
        </ul>
      </Panel>

      {selected && <Deliveries client={client} webhook={selected} />}

      <Panel title="Add a webhook">
        <form onSubmit={(e) => {
          e.preventDefault()
          void run(() => client.request('POST', `/applications/${appId}/webhooks`, {
            name: form.name.trim(), url: form.url.trim(), secret_ref: form.secret_ref.trim(), events: list(form.events), fields: form.fields.trim() ? list(form.fields) : null,
          }), 'Webhook created.').then(() => setForm({ name: '', url: '', secret_ref: '', events: '', fields: '' }))
        }}>
          <Field label="Name"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="URL" hint="https only; internal addresses are refused"><input type="url" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} /></Field>
          <Field label="Signing secret reference" hint="env:NAME or file:/path"><input value={form.secret_ref} onChange={(e) => setForm({ ...form, secret_ref: e.target.value })} /></Field>
          <Field label="Events" hint="Comma-separated patterns such as core.Product.created, core.*.updated"><input value={form.events} onChange={(e) => setForm({ ...form, events: e.target.value })} /></Field>
          <Field label="Fields to send" hint="Empty = ids only. Use * for all fields."><input value={form.fields} onChange={(e) => setForm({ ...form, fields: e.target.value })} /></Field>
          <button type="submit" className="primary" disabled={!form.name.trim() || !form.url.trim() || !form.secret_ref.trim() || list(form.events).length === 0}>Create webhook</button>
        </form>
      </Panel>
    </>
  )
}

function Deliveries({ client, webhook }: { client: ApiClient; webhook: Webhook }) {
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState('')
  const items = useApi(() => client.request<Page<Delivery>>('GET', `/webhooks/${webhook.id}/deliveries?limit=${LIMIT}&offset=${offset}${status ? `&status=${status}` : ''}`), [client, webhook.id, offset, status])
  const { run, error, notice } = useAction(items.reload)
  const [detail, setDetail] = useState<unknown>(null)
  return (
    <Panel title={`Deliveries of ${webhook.name}`} actions={
      <label>Status <select aria-label="Delivery status" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0) }}>
        {['', 'queued', 'sending', 'delivered', 'dead'].map((s) => <option key={s} value={s}>{s || 'any'}</option>)}</select></label>}>
      <Loading loading={items.loading} error={items.error} />
      <Feedback error={error} notice={notice} />
      <table className="trace"><tbody>
        {items.data?.items.map((d) => (
          <tr key={d.id}>
            <td>{d.event_type}</td><td><Pill value={d.status} /></td><td>{d.attempt}/{d.max_attempts}</td><td>{d.response_status ?? '—'}</td><td>{d.error ?? ''}</td><td>{when(d.last_attempt_at)}</td>
            <td>
              <button type="button" className="link" onClick={async () => setDetail(await client.request('GET', `/webhook-deliveries/${d.id}`))}>Payload</button>
              {(d.status === 'dead' || d.status === 'delivered') && <button type="button" className="link" onClick={() => void run(() => client.request('POST', `/webhook-deliveries/${d.id}/retry`), 'Queued again.')}>Resend</button>}
            </td>
          </tr>
        ))}
      </tbody></table>
      <Pager page={items.data} offset={offset} limit={LIMIT} onOffset={setOffset} />
      {detail !== null && <Json value={detail} />}
    </Panel>
  )
}
