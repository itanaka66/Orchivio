import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { mockApi } from './mock'
import { Registry } from './Registry'

const ts = '2026-01-02T03:04:05+00:00'
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 50, offset: 0 })
const pkg = (over = {}) => ({ package: 'acme/inventory-core', name: 'inventory-core', organization: 'acme', version: '1.0.0', kind: 'module', description: 'Stock tracking', license: 'MIT', versions: 2, contents: { modules: ['core'], entities: ['core.Product'], roles: ['admin'] }, ...over })
const installed = (over = {}) => ({ module: 'core', package: 'acme/inventory-core', version: '1.0.0', state: 'installed', latest_version: '1.0.0', update_available: false, renamed_from: null, dependency_of: null, requires: [], required_by: [], ...over })

const show = (api: ReturnType<typeof mockApi>, canPublish = true) =>
  render(<Registry client={api.client} appId="a1" orgId="o1" modules={['core', 'reports']} canPublish={canPublish} />)

describe('Registry: installed modules', () => {
  it('lists what came from the registry with state, requirements and updates', async () => {
    const api = mockApi({ 'GET /applications/:a/registry/installs': { items: [
      installed({ module: 'stock', renamed_from: 'core', update_available: true, latest_version: '1.1.0', required_by: ['reports'] }),
      installed({ module: 'reports', package: 'acme/reports', requires: [{ package: 'acme/inventory-core', range: '^1.0.0' }], state: 'modified' }),
    ] } })
    show(api)
    expect(await screen.findByText(/module core renamed/)).toBeInTheDocument()
    expect(screen.getByText('1.1.0 available', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Required by reports')).toBeInTheDocument()
    expect(screen.getByText('Requires acme/inventory-core ^1.0.0')).toBeInTheDocument()
    expect(screen.getByText('changed after install')).toBeInTheDocument()
    const stock = screen.getByText('stock').closest('li')!
    expect(within(stock).queryByRole('button', { name: 'Remove…' })).toBeDisabled() // something requires it
  })

  it('proposes an upgrade and shows the steps', async () => {
    const api = mockApi({
      'GET /applications/:a/registry/installs': { items: [installed({ update_available: true, latest_version: '1.1.0' })] },
      'POST /applications/:a/registry/install': { proposal: { title: 'Upgrade module acme/inventory-core@1.1.0' }, steps: [{ package: 'acme/inventory-core', version: '1.1.0', module: 'core', action: 'upgrade', dependency: false }] },
    })
    show(api)
    await userEvent.click(await screen.findByRole('button', { name: 'Upgrade to 1.1.0' }))
    expect(await screen.findByText('Upgrade module acme/inventory-core@1.1.0')).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'What will change' })).getByText(/upgrade/)).toBeInTheDocument()
    expect(api.find('POST', '/registry/install')[0]?.body).toEqual({ package: 'acme/inventory-core', version: 'latest' })
  })

  it('removal needs a second click, and a changed module needs an explicit "continue anyway"', async () => {
    let n = 0
    const api = mockApi({
      'GET /applications/:a/registry/installs': { items: [installed()] },
      'POST /applications/:a/registry/uninstall': () => { n += 1; if (n === 1) throw new ApiError(409, 'module_modified', 'module core was changed after it was installed'); return { proposal: { title: 'Remove module core' } } },
    })
    show(api)
    await userEvent.click(await screen.findByRole('button', { name: 'Remove…' }))
    expect(api.find('POST', '/registry/uninstall')).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Propose removal' }))
    expect(await screen.findByRole('button', { name: 'Continue anyway' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Continue anyway' }))
    expect(await screen.findByText('Remove module core')).toBeInTheDocument()
    expect(api.find('POST', '/registry/uninstall').map((c) => c.body)).toEqual([{ module: 'core' }, { module: 'core', overwrite_local_changes: true }])
  })

  it('says so when nothing was installed', async () => {
    show(mockApi({ 'GET /applications/:a/registry/installs': { items: [] } }))
    expect(await screen.findByText('Nothing was installed from the registry.')).toBeInTheDocument()
  })
})

describe('Registry: browsing and installing', () => {
  const base = (extra = {}) => ({
    'GET /applications/:a/registry/installs': { items: [] },
    'GET /registry/packages': page([pkg(), pkg({ package: 'acme/starter', name: 'starter', kind: 'template', description: 'A starter', versions: 1 })]),
    'GET /registry/packages/:org/:name': { package: 'acme/inventory-core', kind: 'module', latest: '1.0.0', versions: [
      { version: '1.0.0', yanked: false, yanked_reason: null, created_at: ts, imported: false, digest: 'a', visibility: 'org' },
      { version: '0.9.0', yanked: true, yanked_reason: 'security bug', created_at: ts, imported: true, digest: 'b', visibility: 'org' }] },
    ...extra,
  })
  const browse = async (api: ReturnType<typeof mockApi>) => { show(api); await userEvent.click(await screen.findByRole('tab', { name: 'Browse' })) }

  it('searches and shows versions, including withdrawn ones with the reason', async () => {
    const api = mockApi(base())
    await browse(api)
    await userEvent.type(screen.getByLabelText('Search packages'), 'stock')
    await waitFor(() => expect(api.calls.some((c) => c.path.includes('q=stock'))).toBe(true))
    await userEvent.selectOptions(screen.getByLabelText('Kind'), 'template')
    await waitFor(() => expect(api.calls.some((c) => c.path.includes('kind=template'))).toBe(true))
    const row = (await screen.findByText('acme/inventory-core')).closest('li')!
    await userEvent.click(within(row).getByRole('button', { name: 'Open' }))
    expect(await screen.findByText(/security bug/)).toBeInTheDocument()
    expect(screen.getByText('imported')).toBeInTheDocument()
  })

  it('installs with a version range and a different module key, and lists what will happen', async () => {
    const api = mockApi(base({ 'POST /applications/:a/registry/install': { proposal: { title: 'Install module acme/reports@1.0.0 (+1 required)' }, steps: [
      { package: 'acme/inventory-core', version: '1.0.0', module: 'stock', action: 'install', dependency: true, renamed_from: 'core' },
      { package: 'acme/reports', version: '1.0.0', module: 'reports', action: 'install', dependency: false }] } }))
    await browse(api)
    const row = (await screen.findByText('acme/inventory-core')).closest('li')!
    await userEvent.click(within(row).getByRole('button', { name: 'Open' }))
    const version = await screen.findByLabelText(/^Version/)
    await userEvent.clear(version)
    await userEvent.type(version, '^1.0.0')
    await userEvent.type(screen.getByLabelText(/^Module key/), 'stock')
    await userEvent.click(screen.getByRole('button', { name: 'Propose installation' }))
    expect(await screen.findByText(/\(\+1 required\)/)).toBeInTheDocument()
    const steps = within(screen.getByRole('list', { name: 'What will change' }))
    expect(steps.getByText(/· required/)).toBeInTheDocument()
    expect(steps.getByText(/was core/)).toBeInTheDocument()
    expect(api.find('POST', '/registry/install')[0]?.body).toEqual({ package: 'acme/inventory-core', version: '^1.0.0', as_module: 'stock' })
  })

  it('shows the server’s refusal with its details', async () => {
    const api = mockApi(base({ 'POST /applications/:a/registry/install': () => { throw new ApiError(409, 'dependency_conflict', "'acme/inventory-core' is needed at ^2.0.0 but 1.0.0 was already selected", { package: 'acme/inventory-core', wanted: '^2.0.0' }) } }))
    await browse(api)
    await userEvent.click(within((await screen.findByText('acme/inventory-core')).closest('li')!).getByRole('button', { name: 'Open' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Propose installation' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('already selected')
    expect(alert).toHaveTextContent('"wanted": "^2.0.0"')
  })

  it('creates an application from a template', async () => {
    const api = mockApi(base({ 'POST /projects/:p/registry/instantiate': { application_id: 'x', key: 'fresh' } }))
    await browse(api)
    await userEvent.click(within((await screen.findByText('acme/starter')).closest('li')!).getByRole('button', { name: 'Open' }))
    await userEvent.type(await screen.findByLabelText('Project id'), 'p1')
    await userEvent.type(screen.getByLabelText('Application key'), 'fresh')
    await userEvent.type(screen.getByLabelText('Name'), 'Fresh app')
    await userEvent.click(screen.getByRole('button', { name: 'Create application' }))
    expect(await screen.findByText(/fresh/, { selector: 'code' })).toBeInTheDocument()
    expect(api.find('POST', '/projects/p1/registry/instantiate')[0]?.body).toEqual({ package: 'acme/starter', version: 'latest', key: 'fresh', name: 'Fresh app' })
  })
})

describe('Registry: publishing and other servers', () => {
  it('hides publishing and other servers from people who cannot publish', async () => {
    show(mockApi({ 'GET /applications/:a/registry/installs': { items: [] } }), false)
    await screen.findByRole('tab', { name: 'Browse' })
    expect(screen.queryByRole('tab', { name: 'Publish' })).toBeNull()
    expect(screen.queryByRole('tab', { name: 'Other servers' })).toBeNull()
  })

  it('publishes a module with its requirements and reports a failure with the validation issues', async () => {
    let n = 0
    const api = mockApi({
      'GET /applications/:a/registry/installs': { items: [] },
      'POST /applications/:a/registry/publish': () => { n += 1; if (n === 1) throw new ApiError(422, 'not_self_contained', 'the module is not self-contained', [{ code: 'unknown-entity', message: 'core.Product' }]); return { package: 'acme/reports', version: '1.0.0' } },
    })
    show(api)
    await userEvent.click(await screen.findByRole('tab', { name: 'Publish' }))
    await userEvent.type(screen.getByLabelText(/^Name/), 'reports')
    await userEvent.selectOptions(screen.getByLabelText('Module'), 'reports')
    await userEvent.type(screen.getByLabelText(/^Requires/), 'acme/inventory-core ^1.0.0, acme/other')
    await userEvent.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('not self-contained')
    expect(screen.getByText(/unknown-entity/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Publish' }))
    expect(await screen.findByText(/Published/)).toHaveTextContent('acme/reports')
    expect(api.find('POST', '/registry/publish')[1]?.body).toEqual({
      name: 'reports', version: '1.0.0', kind: 'module', module: 'reports', requires: [{ package: 'acme/inventory-core', range: '^1.0.0' }, { package: 'acme/other', range: 'latest' }],
      description: '', license: '', visibility: 'org',
    })
  })

  it('publishes a template without module fields', async () => {
    const api = mockApi({ 'GET /applications/:a/registry/installs': { items: [] }, 'POST /applications/:a/registry/publish': { package: 'acme/starter', version: '1.0.0' } })
    show(api)
    await userEvent.click(await screen.findByRole('tab', { name: 'Publish' }))
    await userEvent.type(screen.getByLabelText(/^Name/), 'starter')
    await userEvent.selectOptions(screen.getByLabelText('Kind'), 'template')
    expect(screen.queryByLabelText('Module')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(api.find('POST', '/registry/publish')[0]?.body).toMatchObject({ kind: 'template', name: 'starter' }))
    expect(api.find('POST', '/registry/publish')[0]?.body).not.toHaveProperty('module')
  })

  it('withdraws and restores a version', async () => {
    const api = mockApi({ 'GET /applications/:a/registry/installs': { items: [] }, 'POST /registry/packages/:o/:n/:v/yank': {}, 'POST /registry/packages/:o/:n/:v/unyank': {} })
    show(api)
    await userEvent.click(await screen.findByRole('tab', { name: 'Publish' }))
    await userEvent.type(screen.getByLabelText('Package'), 'acme/inventory-core')
    const panel = screen.getByRole('region', { name: 'Withdraw a version' })
    await userEvent.type(within(panel).getByLabelText('Version'), '1.0.0')
    await userEvent.type(within(panel).getByLabelText('Reason'), 'security bug')
    await userEvent.click(within(panel).getByRole('button', { name: 'Withdraw' }))
    await waitFor(() => expect(api.find('POST', '/yank')[0]?.body).toEqual({ reason: 'security bug' }))
    await userEvent.click(within(panel).getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(api.find('POST', '/unyank')).toHaveLength(1))
  })

  it('manages remote registries, browses and pulls from one, and pushes to it', async () => {
    const remote = { name: 'partner', base_url: 'https://registry.example.org', token_ref: 'env:T', remote_organization_id: 'org-9' }
    const api = mockApi({
      'GET /applications/:a/registry/installs': { items: [] },
      'GET /organizations/:o/registry/remotes': { items: [remote] }, 'PUT /organizations/:o/registry/remotes/:n': remote,
      'GET /organizations/:o/registry/remotes/:n/packages': { items: [pkg({ package: 'partner/billing-kit', name: 'billing-kit' })] },
      'POST /organizations/:o/registry/remotes/:n/pull': { package: 'acme/billing-kit', version: '1.0.0' },
      'POST /organizations/:o/registry/remotes/:n/push': { pushed: true },
    })
    show(api)
    await userEvent.click(await screen.findByRole('tab', { name: 'Other servers' }))
    expect(await screen.findByText(/token env:T/)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Name'), 'second')
    await userEvent.type(screen.getByLabelText(/^Address/), 'https://other.example.org')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.find('PUT', '/remotes/second')[0]?.body).toEqual({ base_url: 'https://other.example.org', token_ref: null, remote_organization_id: null }))
    await userEvent.click(screen.getByRole('button', { name: 'Browse' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Pull' }))
    expect(await screen.findByText('Pulled as acme/billing-kit 1.0.0.')).toBeInTheDocument()
    expect(api.find('POST', '/pull')[0]?.body).toEqual({ package: 'partner/billing-kit' })
    const push = screen.getByRole('region', { name: 'Push one of your packages' })
    await userEvent.type(within(push).getByLabelText('Package'), 'acme/inventory-core')
    await userEvent.type(within(push).getByLabelText('Version'), '1.0.0')
    await userEvent.click(within(push).getByRole('button', { name: 'Push to partner' }))
    await waitFor(() => expect(api.find('POST', '/push')[0]?.body).toEqual({ package: 'acme/inventory-core', version: '1.0.0' }))
  })

  it('shows why a pull was refused', async () => {
    const api = mockApi({
      'GET /applications/:a/registry/installs': { items: [] },
      'GET /organizations/:o/registry/remotes': { items: [{ name: 'partner', base_url: 'https://r.example', token_ref: null, remote_organization_id: null }] },
      'GET /organizations/:o/registry/remotes/:n/packages': { items: [pkg()] },
      'POST /organizations/:o/registry/remotes/:n/pull': () => { throw new ApiError(422, 'untrusted_key', 'the package is signed with a key this server does not trust') },
    })
    show(api)
    await userEvent.click(await screen.findByRole('tab', { name: 'Other servers' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Browse' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Pull' }))
    expect(await screen.findByText(/does not trust/)).toBeInTheDocument()
  })
})
