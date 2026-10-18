import { useEffect, useState } from 'react'
import { ApiError, type ApiClient } from '../api/client'
import { Danger, Feedback, Field, Loading, Panel, Pill, useAction, useApi } from './kit'

interface Provider {
  issuer: string; client_id: string; client_secret_ref: string | null; scopes: string; username_claim: string; role_claim: string
  role_mapping: Record<string, string>; default_roles: string[]; auto_provision: boolean; allowed_email_domains: string[] | null
  post_login_url: string; status: string; login_path: string
}
interface Form {
  issuer: string; client_id: string; client_secret_ref: string; scopes: string; username_claim: string; role_claim: string
  mapping: string; default_roles: string[]; auto_provision: boolean; domains: string; post_login_url: string; status: string
}
const blank: Form = { issuer: '', client_id: '', client_secret_ref: '', scopes: 'openid email profile', username_claim: 'email', role_claim: 'groups', mapping: '', default_roles: [], auto_provision: false, domains: '', post_login_url: '/', status: 'active' }

/** "claim=role" lines → mapping object; null if a line is malformed. */
export function parseMapping(text: string): Record<string, string> | null {
  const out: Record<string, string> = {}
  for (const line of text.split('\n').map((l) => l.trim()).filter(Boolean)) {
    const [claim, role, ...rest] = line.split('=').map((x) => x.trim())
    if (!claim || !role || rest.length) return null
    out[claim] = role
  }
  return out
}

export function Oidc({ client, appId, roles }: { client: ApiClient; appId: string; roles: string[] }) {
  const provider = useApi(async () => {
    try { return await client.request<Provider>('GET', `/applications/${appId}/oidc`) } catch (e) { if (e instanceof ApiError && e.status === 404) return null; throw e }
  }, [client, appId])
  const { run, error, notice } = useAction(provider.reload)
  const [form, setForm] = useState<Form>(blank)
  const [test, setTest] = useState<{ ok: boolean; redirect_uri: string } | null>(null)
  const [testError, setTestError] = useState('')
  const [formError, setFormError] = useState('')

  useEffect(() => {
    const p = provider.data
    if (p) setForm({ issuer: p.issuer, client_id: p.client_id, client_secret_ref: p.client_secret_ref ?? '', scopes: p.scopes, username_claim: p.username_claim, role_claim: p.role_claim,
      mapping: Object.entries(p.role_mapping).map(([k, v]) => `${k}=${v}`).join('\n'), default_roles: p.default_roles, auto_provision: p.auto_provision,
      domains: (p.allowed_email_domains ?? []).join(', '), post_login_url: p.post_login_url, status: p.status })
    else setForm(blank)
  }, [provider.data])

  const save = () => {
    const mapping = parseMapping(form.mapping)
    if (mapping === null) { setFormError('Role mapping lines look like  admins=admin  (provider value = application role).'); return }
    setFormError('')
    const domains = form.domains.split(',').map((d) => d.trim()).filter(Boolean)
    void run(() => client.request('PUT', `/applications/${appId}/oidc`, {
      issuer: form.issuer.trim(), client_id: form.client_id.trim(), client_secret_ref: form.client_secret_ref.trim() || null, scopes: form.scopes, username_claim: form.username_claim,
      role_claim: form.role_claim, role_mapping: mapping, default_roles: form.default_roles, auto_provision: form.auto_provision,
      allowed_email_domains: domains.length ? domains : null, post_login_url: form.post_login_url, status: form.status,
    }), 'Identity provider saved.')
  }

  return (
    <Panel title="OpenID Connect sign-in">
      <p className="hint">Lets this application's end users sign in with an external identity provider (authorization code with PKCE). The client secret is a reference to a secret on the server.</p>
      <Loading loading={provider.loading} error={provider.error} />
      <Feedback error={error} notice={notice} />
      {provider.data && <p>Status <Pill value={provider.data.status} /> · sign-in link <code>{provider.data.login_path}</code></p>}
      <form onSubmit={(e) => { e.preventDefault(); save() }}>
        <Field label="Issuer" hint="Exactly as the provider reports it: https, no trailing slash"><input value={form.issuer} onChange={(e) => setForm({ ...form, issuer: e.target.value })} /></Field>
        <Field label="Client ID"><input value={form.client_id} onChange={(e) => setForm({ ...form, client_id: e.target.value })} /></Field>
        <Field label="Client secret reference" hint="env:NAME or file:/path; empty for a public client"><input value={form.client_secret_ref} onChange={(e) => setForm({ ...form, client_secret_ref: e.target.value })} /></Field>
        <Field label="Scopes"><input value={form.scopes} onChange={(e) => setForm({ ...form, scopes: e.target.value })} /></Field>
        <div className="kv-row">
          <Field label="Username claim"><input value={form.username_claim} onChange={(e) => setForm({ ...form, username_claim: e.target.value })} /></Field>
          <Field label="Role claim"><input value={form.role_claim} onChange={(e) => setForm({ ...form, role_claim: e.target.value })} /></Field>
        </div>
        <Field label="Role mapping" hint="One per line: provider value = application role"><textarea rows={3} value={form.mapping} onChange={(e) => setForm({ ...form, mapping: e.target.value })} /></Field>
        <fieldset><legend>Default roles</legend>{roles.map((r) => (
          <label key={r} className="field check"><input type="checkbox" checked={form.default_roles.includes(r)}
            onChange={() => setForm({ ...form, default_roles: form.default_roles.includes(r) ? form.default_roles.filter((x) => x !== r) : [...form.default_roles, r] })} /><span>{r}</span></label>))}</fieldset>
        <label className="field check"><input type="checkbox" checked={form.auto_provision} onChange={(e) => setForm({ ...form, auto_provision: e.target.checked })} /><span>Create users automatically on first sign-in</span></label>
        {form.auto_provision && <p className="sev-warning">Anyone the provider authenticates (within the allowed domains) gets an account. Restrict the domains.</p>}
        <Field label="Allowed e-mail domains" hint="Comma-separated; empty = any"><input value={form.domains} onChange={(e) => setForm({ ...form, domains: e.target.value })} /></Field>
        <Field label="After sign-in go to"><input value={form.post_login_url} onChange={(e) => setForm({ ...form, post_login_url: e.target.value })} /></Field>
        <Field label="Status"><select value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}><option value="active">active</option><option value="disabled">disabled</option></select></Field>
        {formError && <p role="alert" className="sev-error">{formError}</p>}
        <div className="toolbar">
          <button type="submit" className="primary" disabled={!form.issuer.trim() || !form.client_id.trim()}>Save</button>
          {provider.data && <button type="button" onClick={async () => {
            setTest(null); setTestError('')
            try { setTest(await client.request('POST', `/applications/${appId}/oidc/test`)) } catch (e) { setTestError(e instanceof Error ? e.message : 'Test failed.') }
          }}>Test connection</button>}
          {provider.data && <Danger label="Remove" onConfirm={() => run(() => client.request('DELETE', `/applications/${appId}/oidc`), 'Identity provider removed.')} />}
        </div>
      </form>
      {test && <p role="status">Provider reachable. Register this redirect URI with the provider: <code>{test.redirect_uri}</code></p>}
      {testError && <p role="alert" className="sev-error">{testError}</p>}
    </Panel>
  )
}
