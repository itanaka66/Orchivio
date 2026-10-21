import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ApiError, type Application, type Org, type Project } from '../api/client'
import { AdminWorkspace, modelFacts, visibleSections } from './AdminWorkspace'
import { GitSync } from './GitSync'
import { Governance } from './Governance'
import { Integrations } from './Integrations'
import { Logs } from './Logs'
import { McpServers } from './McpServers'
import { mockApi } from './mock'
import { Oidc, parseMapping } from './Oidc'
import { Processes } from './Processes'

const ts = '2026-01-02T03:04:05+00:00'
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 15, offset: 0 })
const notFound = () => { throw new ApiError(404, 'not_found', 'not found') }

describe('Integrations', () => {
  const dest = { name: 'erp', base_url: 'https://erp.example', auth: { type: 'bearer', header: null, secret_ref: 'env:ERP' }, credential_available: false }
  it('flags a missing credential, saves a header-auth destination and deletes after confirming', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/destinations': { items: [dest] }, 'PUT /applications/:a/destinations/:n': {}, 'DELETE /applications/:a/destinations/:n': undefined })
    render(<Integrations client={client} appId="a1" />)
    expect(await screen.findByText(/credential missing on the server/)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText(/^Name/), 'crm')
    await userEvent.type(screen.getByLabelText(/^Base URL/), 'https://crm.example/api')
    await userEvent.selectOptions(screen.getByLabelText('Authentication'), 'header')
    await userEvent.type(screen.getByLabelText('Header name'), 'X-Api-Key')
    await userEvent.type(screen.getByLabelText(/^Secret reference/), 'env:CRM')
    await userEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    await waitFor(() => expect(find('PUT', '/destinations/crm')[0]?.body).toEqual({ base_url: 'https://crm.example/api', auth: { type: 'header', secret_ref: 'env:CRM', header: 'X-Api-Key' } }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await userEvent.click(screen.getByRole('button', { name: 'Sure?' }))
    await waitFor(() => expect(find('DELETE', '/destinations/erp')).toHaveLength(1))
  })

  it('sends an OpenAPI document as the raw body with key and destination in the query', async () => {
    const { client, calls } = mockApi({ 'GET /applications/:a/destinations': { items: [dest] }, 'POST /applications/:a/integrations/openapi-import': { id: 'p1', title: 'Import OpenAPI as integration core.erp' } })
    render(<Integrations client={client} appId="a1" />)
    await userEvent.type(await screen.findByLabelText(/^Integration key/), 'erp')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Destination/ }), 'erp')
    await userEvent.click(screen.getByLabelText(/OpenAPI \(JSON or YAML\)/))
    await userEvent.paste('openapi: 3.0.0')
    await userEvent.click(screen.getByRole('button', { name: 'Create proposal' }))
    expect(await screen.findByText(/Proposal created/)).toBeInTheDocument()
    const call = calls.find((c) => c.path.includes('openapi-import'))!
    expect(call.path).toBe('/applications/a1/integrations/openapi-import?key=erp&destination=erp')
    expect(call.body).toBe('openapi: 3.0.0')
  })
})

describe('McpServers', () => {
  const server = { id: 's1', name: 'docs', url: 'https://mcp.example', secret_ref: null, status: 'active', discovered_at: ts, tools: [{ name: 'search', description: 'Search docs', enabled: false, level: 'A0' }] }
  it('enables a discovered tool only as read-only (A0) and discovers again', async () => {
    const { client, find } = mockApi({ 'GET /organizations/:o/mcp-servers': { items: [server] }, 'PUT /organizations/:o/mcp-servers/:n/tools/:t': {}, 'POST /organizations/:o/mcp-servers/:n/discover': {} })
    render(<McpServers client={client} orgId="o1" />)
    await userEvent.click(await screen.findByLabelText('Enable search'))
    await waitFor(() => expect(find('PUT', '/tools/search')[0]?.body).toEqual({ enabled: true, level: 'A0' }))
    await userEvent.click(screen.getByRole('button', { name: 'Discover tools' }))
    await waitFor(() => expect(find('POST', '/discover')).toHaveLength(1))
  })

  it('registers a server and shows the server’s refusal', async () => {
    const { client, find } = mockApi({ 'GET /organizations/:o/mcp-servers': { items: [] }, 'POST /organizations/:o/mcp-servers': () => { throw new ApiError(422, 'destination_refused', 'private addresses are not allowed') } })
    render(<McpServers client={client} orgId="o1" />)
    await userEvent.type(await screen.findByLabelText(/^Name/), 'internal')
    await userEvent.type(screen.getByLabelText(/^URL/), 'https://10.0.0.1')
    await userEvent.click(screen.getByRole('button', { name: 'Register' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('private addresses are not allowed')
    expect(find('POST', '/mcp-servers')[0]?.body).toEqual({ name: 'internal', url: 'https://10.0.0.1' })
  })
})

describe('Processes', () => {
  const inst = (over = {}) => ({ id: 'i1', process: 'core.Approval', status: 'failed', initiator: 'u', error: 'step failed', started_at: ts, finished_at: null, next_wake_at: null, ...over })
  it('retries a failed instance, cancels a running one, and shows details', async () => {
    const { client, find } = mockApi({
      'GET /applications/:a/process-instances': page([inst(), inst({ id: 'i2', status: 'running', error: null })]),
      'GET /applications/:a/user-tasks': page([]), 'POST /process-instances/:i/retry': {}, 'POST /process-instances/:i/cancel': {},
      'GET /process-instances/:i': { ...inst(), variables: { amount: 5 }, tokens: [], tasks: [{ id: 't', title: 'Approve', status: 'open', outcome: null }], events: [{ at: ts, type: 'started', step_id: null, actor: 'u' }] },
    })
    render(<Processes client={client} appId="a1" processes={['core.Approval']} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Retry failed step' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(find('POST', '/process-instances/i1/retry')).toHaveLength(1))
    expect(find('POST', '/process-instances/i2/cancel')).toHaveLength(1)
    await userEvent.click(screen.getAllByRole('button', { name: 'Details' })[0]!)
    expect(await screen.findByText(/"amount": 5/)).toBeInTheDocument()
    expect(screen.getByText('Approve')).toBeInTheDocument()
  })

  it('starts an instance with JSON arguments and rejects bad JSON locally', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/process-instances': page([]), 'GET /applications/:a/user-tasks': page([]), 'POST /applications/:a/processes/:p/start': {} })
    render(<Processes client={client} appId="a1" processes={['core.Approval']} />)
    const args = await screen.findByLabelText('Arguments (JSON)')
    await userEvent.clear(args)
    await userEvent.type(args, 'not json')
    await userEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('valid JSON')
    expect(find('POST', '/start')).toHaveLength(0)
    await userEvent.clear(args)
    await userEvent.click(args)
    await userEvent.paste('{"amount": 7}')
    await userEvent.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(find('POST', '/processes/core.Approval/start')[0]?.body).toEqual({ args: { amount: 7 } }))
  })

  it('shows overdue tasks in the monitor', async () => {
    const { client } = mockApi({ 'GET /applications/:a/process-instances': page([]),
      'GET /applications/:a/user-tasks': page([{ id: 't', instance_id: 'i', title: 'Review order', status: 'open', assignee_role: 'editor', assignee_user: null, due_at: ts, overdue: true, escalated_at: null, outcome: null }]) })
    render(<Processes client={client} appId="a1" processes={[]} />)
    await userEvent.click(await screen.findByRole('tab', { name: 'All tasks' }))
    expect(await screen.findByText('Review order')).toBeInTheDocument()
    expect(screen.getByText(/overdue/)).toBeInTheDocument()
  })
})

describe('Oidc', () => {
  it('parses role mappings strictly', () => {
    expect(parseMapping('admins = admin\nstaff=viewer\n')).toEqual({ admins: 'admin', staff: 'viewer' })
    expect(parseMapping('')).toEqual({})
    for (const bad of ['admins', 'a=b=c', '=x', 'x=']) expect(parseMapping(bad)).toBeNull()
  })

  it('starts empty, saves a provider with mapping and domains', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/oidc': notFound, 'PUT /applications/:a/oidc': {} })
    render(<Oidc client={client} appId="a1" roles={['viewer', 'admin']} />)
    await userEvent.type(await screen.findByLabelText(/^Issuer/), 'https://idp.example')
    await userEvent.type(screen.getByLabelText('Client ID'), 'cid')
    await userEvent.type(screen.getByLabelText(/^Client secret reference/), 'env:OIDC')
    await userEvent.click(screen.getByLabelText(/^Role mapping/))
    await userEvent.paste('admins=admin')
    await userEvent.click(screen.getByLabelText('viewer'))
    await userEvent.type(screen.getByLabelText(/^Allowed e-mail domains/), 'example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(find('PUT', '/oidc')[0]?.body).toMatchObject({
      issuer: 'https://idp.example', client_id: 'cid', client_secret_ref: 'env:OIDC', role_mapping: { admins: 'admin' }, default_roles: ['viewer'], allowed_email_domains: ['example.com'], auto_provision: false,
    }))
  })

  it('warns about auto-provisioning and rejects a malformed mapping before sending', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/oidc': notFound })
    render(<Oidc client={client} appId="a1" roles={['viewer']} />)
    await userEvent.type(await screen.findByLabelText(/^Issuer/), 'https://idp.example')
    await userEvent.type(screen.getByLabelText('Client ID'), 'cid')
    await userEvent.click(screen.getByLabelText(/Create users automatically/))
    expect(screen.getByText(/Restrict the domains/)).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText(/^Role mapping/))
    await userEvent.paste('nonsense')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Role mapping lines look like')
    expect(find('PUT', '/oidc')).toHaveLength(0)
  })

  it('loads the existing provider and shows the redirect URI from the connection test', async () => {
    const existing = { issuer: 'https://idp.example', client_id: 'cid', client_secret_ref: null, scopes: 'openid email', username_claim: 'email', role_claim: 'groups', role_mapping: { a: 'admin' },
      default_roles: [], auto_provision: false, allowed_email_domains: ['example.com'], post_login_url: '/', status: 'active', login_path: '/api/v1/apps/a1/auth/oidc/login' }
    const { client } = mockApi({ 'GET /applications/:a/oidc': existing, 'POST /applications/:a/oidc/test': { ok: true, redirect_uri: 'https://studio.example/api/v1/apps/a1/auth/oidc/callback' } })
    render(<Oidc client={client} appId="a1" roles={['admin']} />)
    expect(await screen.findByDisplayValue('https://idp.example')).toBeInTheDocument()
    expect(screen.getByDisplayValue('a=admin')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
    expect(await screen.findByText(/oidc\/callback/)).toBeInTheDocument()
  })
})

describe('Governance', () => {
  const pack = { id: 'p1', name: 'baseline', description: 'Org baseline', status: 'active', rules: [{ rule: 'no-anonymous-access', severity: 'error' }] }
  const routes = {
    'GET /organizations/:o/policy-packs': { items: [pack] },
    'GET /governance/policy-rules': { rules: [{ rule: 'no-anonymous-access', description: 'anonymous access stays off' }] },
  }
  it('creates a pack from JSON rules and shows the server’s rule errors', async () => {
    let first = true
    const { client, find } = mockApi({ ...routes, 'POST /organizations/:o/policy-packs': () => { if (first) { first = false; throw new ApiError(422, 'invalid_rules', 'unknown rule', { catalogue: {} }) } return {} } })
    render(<Governance client={client} orgId="o1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'New pack' }))
    await userEvent.type(screen.getByLabelText('Name'), 'strict')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown rule')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(find('POST', '/policy-packs')).toHaveLength(2))
    expect(find('POST', '/policy-packs')[1]?.body).toMatchObject({ name: 'strict', rules: [{ rule: 'no-anonymous-access', severity: 'error' }] })
  })

  it('refuses invalid JSON locally and disables a pack', async () => {
    const { client, find } = mockApi({ ...routes, 'PATCH /organizations/:o/policy-packs/:p': {} })
    render(<Governance client={client} orgId="o1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const rules = screen.getByLabelText('Rules (JSON)')
    await userEvent.clear(rules)
    await userEvent.click(rules)
    await userEvent.paste('{broken')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('valid JSON')
    expect(find('PATCH', '/policy-packs')).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await userEvent.click(screen.getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(find('PATCH', '/policy-packs/p1')[0]?.body).toEqual({ status: 'disabled' }))
  })

  it('shows compliance violations, inventory risks, AI usage and builds export links', async () => {
    const { client } = mockApi({
      ...routes,
      'GET /organizations/:o/governance/compliance': { packs: ['baseline'], summary: { violations: 1 }, applications: [{ application_id: 'a1', project: 'inv', application: 'inventory', status: 'violations',
        violations: [{ rule: 'no-anonymous-access', severity: 'error', message: 'anonymous access is on', pack: 'baseline' }] }] },
      'GET /organizations/:o/governance/inventory': { applications: [{ application_id: 'a1', project: 'inv', application: 'inventory', revision_no: 3, entities: 4, flows: 2, app_users: 7, risks: ['anonymous-access'] }] },
      'GET /organizations/:o/governance/ai-usage': { totals: { jobs: 5, input_tokens: 100, output_tokens: 50, cost_usd: 0.1234 }, mcp_tool_calls: 2, by_project: { inv: { jobs: 5, cost_usd: 0.1234 } }, by_type: { analyze_logs: { jobs: 5, cost_usd: 0.1234 } } },
    })
    render(<Governance client={client} orgId="o1" />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Compliance' }))
    expect(await screen.findByText(/anonymous access is on/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Inventory' }))
    expect(await screen.findByText('anonymous-access')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'AI usage' }))
    expect(await screen.findByText('$0.1234', { selector: 'dd' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Audit export' }))
    await userEvent.type(screen.getByLabelText(/^Action starts with/), 'deployment.')
    expect(screen.getByRole('link', { name: 'Download CSV' })).toHaveAttribute('href', '/api/v1/organizations/o1/governance/audit-export?format=csv&action_prefix=deployment.')
  })
})

describe('GitSync', () => {
  const cfg = { remote_url: 'https://github.com/o/r', base_branch: 'main', push_branch: 'orchivio/inv', path: '', token_ref: 'env:T', provider: 'github', last_push_at: null, last_push_commit: null, last_pull_at: null, last_pull_commit: null }
  it('connects a repository', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/git-sync': notFound, 'PUT /applications/:a/git-sync': {} })
    render(<GitSync client={client} appId="a1" />)
    await userEvent.type(await screen.findByLabelText(/^Repository URL/), 'https://github.com/o/r')
    await userEvent.type(screen.getByLabelText(/^Token reference/), 'env:T')
    await userEvent.selectOptions(screen.getByLabelText('Provider'), 'github')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(find('PUT', '/git-sync')[0]?.body).toEqual({ remote_url: 'https://github.com/o/r', base_branch: 'main', push_branch: null, path: '', token_ref: 'env:T', provider: 'github' }))
    expect(screen.queryByRole('button', { name: 'Push current revision' })).toBeNull() // nothing to synchronise until saved
  })

  it('pushes and shows the pull request, including a failed pull request after a good push', async () => {
    let n = 0
    const { client, find } = mockApi({ 'GET /applications/:a/git-sync': cfg, 'POST /applications/:a/git-sync/push': () => {
      n += 1
      return n === 1 ? { changed: true, commit: 'a'.repeat(40), branch: 'orchivio/inv', pull_request: 'https://github.com/o/r/pull/4', pull_request_error: null, note: null }
        : { changed: true, commit: 'b'.repeat(40), branch: 'orchivio/inv', pull_request: null, pull_request_error: 'HTTP 500', note: null }
    } })
    render(<GitSync client={client} appId="a1" />)
    await userEvent.click(await screen.findByLabelText(/Open a pull request/))
    await userEvent.click(screen.getByRole('button', { name: 'Push current revision' }))
    expect(await screen.findByRole('link', { name: 'https://github.com/o/r/pull/4' })).toHaveAttribute('rel', 'noreferrer noopener')
    expect(find('POST', '/push')[0]?.body).toEqual({ open_pull_request: true })
    await userEvent.click(screen.getByRole('button', { name: 'Push current revision' }))
    expect(await screen.findByText(/pull request failed: HTTP 500/)).toBeInTheDocument()
  })

  it('pulls as a proposal and shows refusals', async () => {
    let n = 0
    const { client } = mockApi({ 'GET /applications/:a/git-sync': { ...cfg, provider: 'git' }, 'POST /applications/:a/git-sync/pull': () => {
      n += 1
      if (n === 1) return { title: 'Git sync: main@abc1234' }
      throw new ApiError(409, 'no_changes', "the repository's model equals the application's current model")
    } })
    render(<GitSync client={client} appId="a1" />)
    expect(await screen.findByLabelText(/Open a pull request/)).toBeDisabled() // plain Git has no pull request API
    await userEvent.click(screen.getByRole('button', { name: /Pull main as a proposal/ }))
    expect(await screen.findByText(/Git sync: main@abc1234/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Pull main as a proposal/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('equals the application')
  })
})

describe('Logs', () => {
  const line = (id: number, level: string, message: string) => ({ id, time: ts, level, source: 'job', message, trace_id: null, fields: { outcome: 'dead' } })
  it('filters, pages older lines and shows fields', async () => {
    const { client, calls } = mockApi({ 'GET /applications/:a/logs': (_b: unknown, path: string) => path.includes('before_id=') ? { items: [line(1, 'info', 'old line')], next_before_id: null } : { items: [line(3, 'error', 'job dead'), line(2, 'warning', 'job retry')], next_before_id: 2 } })
    render(<Logs client={client} appId="a1" />)
    expect(await screen.findByText('job dead')).toBeInTheDocument()
    expect(screen.getAllByText('outcome=dead')).toHaveLength(2)
    await userEvent.click(screen.getByRole('button', { name: 'Older lines' }))
    expect(await screen.findByText('old line')).toBeInTheDocument()
    expect(screen.getByText('job dead')).toBeInTheDocument() // earlier lines stay
    await userEvent.selectOptions(screen.getByLabelText('Level'), 'error')
    await waitFor(() => expect(calls.some((c) => c.path.includes('level=error'))).toBe(true))
  })

  it('analyses with the AI and highlights the cited lines', async () => {
    const { client, find } = mockApi({
      'GET /applications/:a/logs': { items: [line(3, 'error', 'job dead')], next_before_id: null },
      'POST /applications/:a/logs/analyze': { lines_analyzed: 1, summary: 'A job keeps dying', dropped_findings: 1, note: 'AI-generated',
        findings: [{ title: 'Dead job', severity: 'high', explanation: 'It fails every time', suggestion: 'Check the flow', evidence_ids: [3] }] },
    })
    render(<Logs client={client} appId="a1" />)
    await screen.findByText('job dead')
    await userEvent.type(screen.getByLabelText('Question'), 'why?')
    await userEvent.click(screen.getByRole('button', { name: /Analyse warnings/ }))
    expect(await screen.findByText('A job keeps dying')).toBeInTheDocument()
    expect(screen.getByText(/1 finding\(s\) were dropped/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Show lines #3' }))
    expect(document.querySelector('tr[data-line="3"]')).toHaveClass('hl')
    expect(find('POST', '/logs/analyze')[0]?.body).toEqual({ minutes: 60, level: 'warning', question: 'why?' })
  })

  it('shows why an analysis could not run', async () => {
    const { client } = mockApi({ 'GET /applications/:a/logs': { items: [], next_before_id: null }, 'POST /applications/:a/logs/analyze': () => { throw new ApiError(503, 'ai_not_configured', 'no AI provider is configured on this server') } })
    render(<Logs client={client} appId="a1" />)
    await userEvent.click(await screen.findByRole('button', { name: /Analyse warnings/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no AI provider is configured')
  })
})

describe('AdminWorkspace', () => {
  const org = (role: string): Org => ({ id: 'o1', name: 'Acme', slug: 'acme', role })
  const project: Project = { id: 'p1', organization_id: 'o1', name: 'Inventory', slug: 'inv' }
  const app: Application = { id: 'a1', project_id: 'p1', key: 'inventory', name: 'Inventory app', current_revision_id: 'r1', current_revision_no: 1 }
  const model = { security: { roles: ['viewer', 'admin'] }, modules: [{ key: 'core', processes: [{ key: 'Approval' }] }] }

  it('hides screens the role cannot use and the application screens when no application is chosen', () => {
    const keys = (role: string, hasApp: boolean) => visibleSections(role, hasApp).map((s) => s.key)
    expect(keys('viewer', true)).toEqual([])
    expect(keys('editor', true)).toEqual(['branches', 'delivery', 'jobs', 'processes', 'tests', 'logs', 'registry'])
    expect(keys('admin', true)).toContain('webhooks')
    expect(keys('admin', false)).toEqual(['mcp', 'governance'])
  })

  it('reads roles and processes from the model', () => {
    expect(modelFacts(model)).toEqual({ roles: ['viewer', 'admin'], processes: ['core.Approval'] })
    expect(modelFacts(null)).toEqual({ roles: [], processes: [] })
  })

  it('navigates between screens and passes the application to them', async () => {
    const { client, calls } = mockApi({
      'GET /applications/:a/branches': [], 'GET /applications/:a/revisions': { items: [] }, 'GET /applications/:a/webhooks': { items: [] }, 'GET /organizations/:o/mcp-servers': { items: [] },
    })
    render(<AdminWorkspace client={client} org={org('admin')} project={project} application={app} model={model} />)
    expect(await screen.findByRole('region', { name: 'Branches' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Webhooks' }))
    expect(await screen.findByRole('region', { name: 'Outbound webhooks' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'MCP servers' }))
    expect(await screen.findByRole('region', { name: 'External MCP servers' })).toBeInTheDocument()
    expect(calls.some((c) => c.path === '/organizations/o1/mcp-servers')).toBe(true)
  })

  it('offers only organization screens without an application and tells a viewer there is nothing', () => {
    const { client } = mockApi({ 'GET /organizations/:o/mcp-servers': { items: [] } })
    const { unmount } = render(<AdminWorkspace client={client} org={org('admin')} project={project} model={null} />)
    expect(screen.getByText('Choose an application to see its screens.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Branches' })).toBeNull()
    unmount()
    render(<AdminWorkspace client={client} org={org('viewer')} project={project} application={app} model={model} />)
    expect(screen.getByText('Nothing here is available for your role.')).toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Administration' })).queryAllByRole('button')).toHaveLength(0)
  })
})
