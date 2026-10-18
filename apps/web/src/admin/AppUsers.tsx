import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Danger, Feedback, Field, Loading, Pager, Panel, Pill, useAction, useApi, when, type Page } from './kit'

interface AppUser { id: string; username: string; display_name: string | null; status: string; roles: string[]; must_change_password: boolean; last_login_at: string | null }
const LIMIT = 25

export function AppUsers({ client, appId, roles }: { client: ApiClient; appId: string; roles: string[] }) {
  const [offset, setOffset] = useState(0)
  const users = useApi(() => client.request<Page<AppUser>>('GET', `/applications/${appId}/app-users?limit=${LIMIT}&offset=${offset}`), [client, appId, offset])
  const { run, error, notice } = useAction(users.reload)
  const [form, setForm] = useState({ username: '', display_name: '', password: '', roles: [] as string[] })
  const [editing, setEditing] = useState<AppUser | null>(null)
  const [reset, setReset] = useState<{ id: string; password: string } | null>(null)

  const toggle = (list: string[], r: string) => (list.includes(r) ? list.filter((x) => x !== r) : [...list, r])

  return (
    <>
      <Panel title="Application users">
        <p className="hint">The end users of this application. They sign in to the application, not to Studio.</p>
        <Loading loading={users.loading} error={users.error} />
        <Feedback error={error} notice={notice} />
        <table className="trace">
          <thead><tr><th>User</th><th>Roles</th><th>Status</th><th>Last sign-in</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {users.data?.items.map((u) => (
              <tr key={u.id}>
                <td>{u.username}{u.display_name ? <small className="hint"> · {u.display_name}</small> : null}{u.must_change_password && <small className="hint"> · must change password</small>}</td>
                <td>{u.roles.map((r) => <span key={r} className="chip">{r}</span>)}</td>
                <td><Pill value={u.status} /></td>
                <td>{when(u.last_login_at)}</td>
                <td>
                  <button type="button" className="link" onClick={() => setEditing(u)}>Edit</button>
                  <button type="button" className="link" onClick={() => void run(() => client.request('PATCH', `/app-users/${u.id}`, { status: u.status === 'active' ? 'disabled' : 'active' }))}>{u.status === 'active' ? 'Disable' : 'Enable'}</button>
                  <button type="button" className="link" onClick={() => setReset({ id: u.id, password: '' })}>Reset password</button>
                  <Danger label="Delete" onConfirm={() => run(() => client.request('DELETE', `/app-users/${u.id}`), `${u.username} deleted.`)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pager page={users.data} offset={offset} limit={LIMIT} onOffset={setOffset} />
      </Panel>

      {editing && (
        <Panel title={`Edit ${editing.username}`}>
          <Field label="Display name"><input value={editing.display_name ?? ''} onChange={(e) => setEditing({ ...editing, display_name: e.target.value })} /></Field>
          <fieldset><legend>Roles</legend>{roles.map((r) => (
            <label key={r} className="field check"><input type="checkbox" checked={editing.roles.includes(r)} onChange={() => setEditing({ ...editing, roles: toggle(editing.roles, r) })} /><span>{r}</span></label>))}</fieldset>
          <div className="toolbar">
            <button type="button" className="primary" onClick={() => void run(() => client.request('PATCH', `/app-users/${editing.id}`, { display_name: editing.display_name ?? '', roles: editing.roles }), 'Saved.').then(() => setEditing(null))}>Save</button>
            <button type="button" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </Panel>
      )}

      {reset && (
        <Panel title="Reset password">
          <Field label="New password" hint="The person must change it at their next sign-in. All their sessions end."><input type="password" autoComplete="new-password" value={reset.password} onChange={(e) => setReset({ ...reset, password: e.target.value })} /></Field>
          <div className="toolbar">
            <button type="button" className="primary" disabled={!reset.password} onClick={() => void run(() => client.request('POST', `/app-users/${reset.id}/reset-password`, { password: reset.password }), 'Password reset.').then(() => setReset(null))}>Reset</button>
            <button type="button" onClick={() => setReset(null)}>Cancel</button>
          </div>
        </Panel>
      )}

      <Panel title="Add a user">
        <form onSubmit={(e) => { e.preventDefault(); void run(() => client.request('POST', `/applications/${appId}/app-users`, { username: form.username.trim(), display_name: form.display_name || undefined, password: form.password, roles: form.roles }), 'User created.').then(() => setForm({ username: '', display_name: '', password: '', roles: [] })) }}>
          <Field label="Username" hint="Lower-case letters, digits and . _ @ + -"><input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></Field>
          <Field label="Display name"><input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></Field>
          <Field label="Initial password"><input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
          <fieldset><legend>Roles</legend>{roles.length === 0 && <small className="hint">The model defines no roles yet.</small>}{roles.map((r) => (
            <label key={r} className="field check"><input type="checkbox" checked={form.roles.includes(r)} onChange={() => setForm({ ...form, roles: toggle(form.roles, r) })} /><span>{r}</span></label>))}</fieldset>
          <button type="submit" className="primary" disabled={!form.username.trim() || !form.password}>Create user</button>
        </form>
      </Panel>
    </>
  )
}
