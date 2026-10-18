import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { AppUsers } from './AppUsers'
import { Branches } from './Branches'
import { Delivery } from './Delivery'
import { Jobs } from './Jobs'
import { mockApi } from './mock'
import { Webhooks } from './Webhooks'

const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 20, offset: 0 })
const ts = '2026-01-02T03:04:05+00:00'

describe('Branches', () => {
  const branches = [
    { id: 'b0', name: 'main', status: 'open', head_revision_id: 'r2', created_at: ts },
    { id: 'b1', name: 'feature-x', status: 'open', head_revision_id: 'r3', created_at: ts },
    { id: 'b2', name: 'old', status: 'merged', head_revision_id: 'r1', created_at: ts },
  ]
  it('lists branches, offers merge/abandon only for open non-main branches, and creates one', async () => {
    const { client, find } = mockApi({
      'GET /applications/:a/branches': branches,
      'GET /applications/:a/revisions': { items: [{ id: 'r1', revision_no: 1, message: 'first', created_at: ts }] },
      'POST /applications/:a/branches': { id: 'b9' },
    })
    render(<Branches client={client} appId="a1" />)
    expect(await screen.findByText('feature-x')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Merge into main…' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Abandon' })).toHaveLength(1)
    await userEvent.type(screen.getByLabelText('Name'), 'idea')
    await userEvent.selectOptions(screen.getByLabelText(/Start from/), 'r1')
    await userEvent.click(screen.getByRole('button', { name: 'Create branch' }))
    expect(find('POST', '/branches')[0]?.body).toEqual({ name: 'idea', from_revision_id: 'r1' })
  })

  it('asks for a decision on conflicts, then merges with the chosen resolutions', async () => {
    let attempt = 0
    const { client, find } = mockApi({
      'GET /applications/:a/branches': branches,
      'GET /applications/:a/revisions': { items: [] },
      'POST /branches/:b/merge': () => {
        attempt += 1
        if (attempt === 1) throw new ApiError(409, 'merge_conflicts', '1 conflict(s) need a resolution', [{ id: 'c1', path: '/modules/0/entities/Product', message: 'both changed' }])
        return { status: 'merged', revision_no: 5, conflicts_resolved: 1, level: 'A2', changes: [{ summary: 'entity Product changed' }] }
      },
    })
    render(<Branches client={client} appId="a1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Merge into main…' }))
    await userEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect(await screen.findByText(/Conflicts need a decision/)).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText('Resolution for c1'), 'theirs')
    await userEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect(await screen.findByText(/revision 5/)).toBeInTheDocument()
    const body = find('POST', '/merge')[1]?.body as { resolutions: Record<string, string>; dry_run: boolean }
    expect(body.resolutions).toEqual({ c1: 'theirs' })
    expect(body.dry_run).toBe(false)
  })

  it('previews a merge without applying it', async () => {
    const { client, find } = mockApi({
      'GET /applications/:a/branches': branches, 'GET /applications/:a/revisions': { items: [] },
      'POST /branches/:b/merge': { status: 'clean', revision_no: null, conflicts_resolved: 0, level: 'A1', changes: [] },
    })
    render(<Branches client={client} appId="a1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Merge into main…' }))
    await userEvent.click(screen.getByRole('button', { name: 'Preview merge' }))
    expect(await screen.findByText(/no revision created/)).toBeInTheDocument()
    expect((find('POST', '/merge')[0]?.body as { dry_run: boolean }).dry_run).toBe(true)
  })

  it('needs a second click to abandon a branch', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/branches': branches, 'GET /applications/:a/revisions': { items: [] }, 'DELETE /branches/:b': undefined })
    render(<Branches client={client} appId="a1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Abandon' }))
    expect(find('DELETE', '/branches')).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Sure?' }))
    await waitFor(() => expect(find('DELETE', '/branches/b1')).toHaveLength(1))
  })
})

describe('Jobs', () => {
  const job = (over = {}) => ({ id: 'j1', flow: 'core.Reorder', trigger: 'manual', status: 'dead', attempt: 3, max_attempts: 3, run_as_role: 'admin', created_at: ts, finished_at: ts, error: 'boom', result: {}, ...over })
  it('shows schedules and jobs, retries a dead job and cancels a queued one', async () => {
    const { client, find } = mockApi({
      'GET /applications/:a/schedules': { items: [{ flow: 'core.Reorder', cron: '*/5 * * * *', enabled: true, run_as_role: 'admin', next_run_at: ts, last_run: { status: 'succeeded' } }] },
      'GET /applications/:a/jobs': page([job(), job({ id: 'j2', status: 'queued', error: null })]),
      'POST /jobs/:j/retry': {}, 'POST /jobs/:j/cancel': {},
    })
    render(<Jobs client={client} appId="a1" roles={['viewer', 'admin']} />)
    expect(await screen.findByText('*/5 * * * *')).toBeInTheDocument()
    expect(await screen.findByText('boom')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(find('POST', '/jobs/j1/retry')).toHaveLength(1))
    expect(find('POST', '/jobs/j2/cancel')).toHaveLength(1)
  })

  it('queues a flow as a chosen role and filters by status', async () => {
    const { client, find, calls } = mockApi({ 'GET /applications/:a/schedules': { items: [] }, 'GET /applications/:a/jobs': page([]), 'POST /applications/:a/jobs': {} })
    render(<Jobs client={client} appId="a1" roles={['viewer', 'admin']} />)
    await userEvent.type(await screen.findByLabelText('Server flow'), 'core.Reorder')
    await userEvent.selectOptions(screen.getByLabelText('Run as role'), 'admin')
    await userEvent.click(screen.getByRole('button', { name: 'Queue' }))
    await waitFor(() => expect(find('POST', '/jobs')[0]?.body).toEqual({ flow: 'core.Reorder', run_as_role: 'admin' }))
    await userEvent.selectOptions(screen.getByLabelText('Filter by status'), 'dead')
    await waitFor(() => expect(calls.some((c) => c.path.includes('status=dead'))).toBe(true))
  })

  it('shows the server’s refusal when queueing fails', async () => {
    const { client } = mockApi({ 'GET /applications/:a/schedules': { items: [] }, 'GET /applications/:a/jobs': page([]),
      'POST /applications/:a/jobs': () => { throw new ApiError(422, 'not_runnable', 'only server flows can be queued') } })
    render(<Jobs client={client} appId="a1" roles={['admin']} />)
    await userEvent.type(await screen.findByLabelText('Server flow'), 'core.X')
    await userEvent.click(screen.getByRole('button', { name: 'Queue' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('only server flows can be queued')
  })
})

describe('AppUsers', () => {
  const users = [{ id: 'u1', username: 'ann', display_name: 'Ann', status: 'active', roles: ['viewer'], must_change_password: true, last_login_at: null }]
  it('lists users, creates one with roles, and toggles status', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/app-users': page(users), 'POST /applications/:a/app-users': {}, 'PATCH /app-users/:u': {} })
    render(<AppUsers client={client} appId="a1" roles={['viewer', 'editor']} />)
    expect(await screen.findByText('ann')).toBeInTheDocument()
    expect(screen.getByText(/must change password/)).toBeInTheDocument()
    const form = screen.getByRole('button', { name: 'Create user' }).closest('form')!
    await userEvent.type(within(form).getByLabelText(/^Username/), 'bob')
    await userEvent.type(within(form).getByLabelText('Initial password'), 'a long passphrase')
    await userEvent.click(within(form).getByLabelText('editor'))
    await userEvent.click(screen.getByRole('button', { name: 'Create user' }))
    await waitFor(() => expect(find('POST', '/app-users')[0]?.body).toEqual({ username: 'bob', password: 'a long passphrase', roles: ['editor'] }))
    await userEvent.click(screen.getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(find('PATCH', '/app-users/u1')[0]?.body).toEqual({ status: 'disabled' }))
  })

  it('edits roles, resets a password and deletes only after confirming', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/app-users': page(users), 'PATCH /app-users/:u': {}, 'POST /app-users/:u/reset-password': {}, 'DELETE /app-users/:u': undefined })
    render(<AppUsers client={client} appId="a1" roles={['viewer', 'editor']} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const panel = screen.getByRole('region', { name: 'Edit ann' })
    await userEvent.click(within(panel).getByLabelText('editor'))
    await userEvent.click(within(panel).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(find('PATCH', '/app-users/u1')[0]?.body).toEqual({ display_name: 'Ann', roles: ['viewer', 'editor'] }))
    await userEvent.click(screen.getByRole('button', { name: 'Reset password' }))
    await userEvent.type(screen.getByLabelText(/^New password/), 'another long passphrase')
    await userEvent.click(within(screen.getByRole('region', { name: 'Reset password' })).getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(find('POST', '/reset-password')[0]?.body).toEqual({ password: 'another long passphrase' }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(find('DELETE', '/app-users')).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Sure?' }))
    await waitFor(() => expect(find('DELETE', '/app-users/u1')).toHaveLength(1))
  })
})

describe('Webhooks', () => {
  const hook = { id: 'w1', name: 'erp', url: 'https://x', secret_ref: 'env:S', events: ['core.Product.created'], fields: null, status: 'paused', paused_reason: 'paused after 5 failures', consecutive_failures: 5 }
  it('shows why a webhook is paused, resumes it, sends a test and creates a new one', async () => {
    const { client, find } = mockApi({ 'GET /applications/:a/webhooks': { items: [hook] }, 'PATCH /webhooks/:w': {}, 'POST /webhooks/:w/test': {}, 'POST /applications/:a/webhooks': {} })
    render(<Webhooks client={client} appId="a1" />)
    expect(await screen.findByText(/paused after 5 failures/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await userEvent.click(screen.getByRole('button', { name: 'Send test' }))
    await waitFor(() => expect(find('PATCH', '/webhooks/w1')[0]?.body).toEqual({ status: 'active' }))
    expect(find('POST', '/webhooks/w1/test')).toHaveLength(1)
    await userEvent.type(screen.getByLabelText('Name'), 'crm')
    await userEvent.type(screen.getByLabelText(/^URL/), 'https://crm.example/hook')
    await userEvent.type(screen.getByLabelText(/Signing secret/), 'env:CRM')
    await userEvent.type(screen.getByLabelText(/^Events/), 'core.*.updated, core.Order.created')
    await userEvent.click(screen.getByRole('button', { name: 'Create webhook' }))
    await waitFor(() => expect(find('POST', '/applications/a1/webhooks')[0]?.body).toEqual({ name: 'crm', url: 'https://crm.example/hook', secret_ref: 'env:CRM', events: ['core.*.updated', 'core.Order.created'], fields: null }))
  })

  it('lists deliveries, shows a payload and resends a dead one', async () => {
    const { client, find } = mockApi({
      'GET /applications/:a/webhooks': { items: [{ ...hook, status: 'active', paused_reason: null }] },
      'GET /webhooks/:w/deliveries': page([{ id: 'd1', event_type: 'core.Product.created', status: 'dead', attempt: 6, max_attempts: 6, response_status: 500, error: 'HTTP 500', delivered_at: null, last_attempt_at: ts }]),
      'GET /webhook-deliveries/:d': { payload: { id: 'evt-1' } }, 'POST /webhook-deliveries/:d/retry': {},
    })
    render(<Webhooks client={client} appId="a1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Deliveries' }))
    expect(await screen.findByText('HTTP 500')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Payload' }))
    expect(await screen.findByText(/evt-1/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Resend' }))
    await waitFor(() => expect(find('POST', '/webhook-deliveries/d1/retry')).toHaveLength(1))
  })

  it('does not create a webhook without events', async () => {
    const { client } = mockApi({ 'GET /applications/:a/webhooks': { items: [] } })
    render(<Webhooks client={client} appId="a1" />)
    await userEvent.type(await screen.findByLabelText('Name'), 'x')
    await userEvent.type(screen.getByLabelText(/^URL/), 'https://x.example')
    await userEvent.type(screen.getByLabelText(/Signing secret/), 'env:X')
    expect(screen.getByRole('button', { name: 'Create webhook' })).toBeDisabled()
  })
})

describe('Delivery', () => {
  const env = (over = {}) => ({ id: 'e1', name: 'test', stage_order: 1, requires_approval: true, require_tests: true, current: null, current_package: null, ...over })
  const pkg = { id: 'p1', revision_id: 'r1', runtime_version: '0.1.0', model_hash: 'abcdef0123456789', digest: 'f'.repeat(64), created_at: ts }
  const base = (extra = {}) => ({
    'GET /applications/:a/environments': { items: [env()] }, 'GET /applications/:a/packages': page([pkg]), 'GET /applications/:a/deployments': page([]), ...extra,
  })

  it('builds a package of the current revision', async () => {
    const { client, find } = mockApi(base({ 'POST /revisions/:r/packages': {} }))
    render(<Delivery client={client} appId="a1" currentRevisionId="r9" />)
    await userEvent.click(await screen.findByRole('button', { name: /Build package/ }))
    await waitFor(() => expect(find('POST', '/revisions/r9/packages')).toHaveLength(1))
  })

  it('explains a promotion that needs acknowledgement, then retries with it', async () => {
    let n = 0
    const { client, find } = mockApi(base({
      'POST /packages/:p/promote': () => {
        n += 1
        if (n === 1) throw new ApiError(409, 'approval_required', 'the migration removes or narrows data', { destructive_steps: [{ op: 'drop_column', column: 'sku' }] })
        return {}
      },
    }))
    render(<Delivery client={client} appId="a1" currentRevisionId="r1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Promote…' }))
    await userEvent.click(screen.getByRole('button', { name: 'Request deployment' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('removes or narrows data')
    expect(screen.getByText(/drop_column/)).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText(/destructive migration steps/))
    await userEvent.click(screen.getByRole('button', { name: 'Request deployment' }))
    await waitFor(() => expect(find('POST', '/promote')[1]?.body).toEqual({ environment: 'test', acknowledge_risks: true }))
  })

  it('approves or rejects a pending deployment', async () => {
    const { client, find } = mockApi(base({
      'GET /applications/:a/deployments': page([{ id: 'd1', environment_id: 'e1', package_id: 'p1', status: 'pending_approval', comment: 'go', created_at: ts, requested_by: 'u' }]),
      'POST /deployments/:d/approval': {},
    }))
    render(<Delivery client={client} appId="a1" currentRevisionId="r1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(find('POST', '/deployments/d1/approval')[0]?.body).toEqual({ decision: 'approve' }))
  })

  it('verifies a package and links the download', async () => {
    const { client } = mockApi(base({ 'GET /packages/:p/verify': { valid: true, digest: 'abc' } }))
    render(<Delivery client={client} appId="a1" currentRevisionId="r1" />)
    expect(await screen.findByRole('link', { name: 'Download' })).toHaveAttribute('href', '/api/v1/packages/p1/download')
    await userEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByText(/"valid": true/)).toBeInTheDocument()
  })

  it('stores secrets as references, never as values, and warns on secret-looking names', async () => {
    const { client, find } = mockApi(base({
      'GET /environments/:e/constants': { items: [{ key: 'API_URL', value: 'https://x', secret_ref: null }] }, 'PUT /environments/:e/constants/:k': {},
    }))
    render(<Delivery client={client} appId="a1" currentRevisionId="r1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Constants' }))
    expect(await screen.findByText('API_URL')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Key'), 'PAYMENT_TOKEN')
    expect(screen.getByText(/looks like a secret/)).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText('Secret reference'))
    await userEvent.type(screen.getByLabelText(/^Secret reference$/, { selector: 'input[type=text], input:not([type])' }), 'env:PAY')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(find('PUT', '/constants/PAYMENT_TOKEN')[0]?.body).toEqual({ secret_ref: 'env:PAY' }))
  })

  it('creates an environment', async () => {
    const { client, find } = mockApi(base({ 'POST /applications/:a/environments': {} }))
    render(<Delivery client={client} appId="a1" currentRevisionId="r1" />)
    await userEvent.type(await screen.findByLabelText('New environment'), 'prod')
    await userEvent.clear(screen.getByLabelText('Stage order'))
    await userEvent.type(screen.getByLabelText('Stage order'), '3')
    await userEvent.click(screen.getByLabelText('Needs approval'))
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(find('POST', '/environments')[0]?.body).toEqual({ name: 'prod', stage_order: 3, requires_approval: true, require_tests: true }))
  })
})
