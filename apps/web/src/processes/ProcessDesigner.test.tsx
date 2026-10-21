import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ApiClient } from '../api/client'
import { ProcessDesigner } from './ProcessDesigner'

const model = {
  security: { roles: ['viewer', 'editor', 'admin'] },
  modules: [{
    key: 'core',
    entities: [{ name: 'Order', fields: [{ name: 'id', type: 'uuid', primary_key: true }] }],
    flows: [{ key: 'NotifyUser', kind: 'server' }],
    processes: [{
      key: 'Approval',
      description: 'Approve a request',
      start_roles: ['viewer', 'editor'],
      run_as_role: 'admin',
      params: [],
      steps: [
        { id: 'start', type: 'start', config: {} },
        { id: 'review', type: 'user_task', config: { title: 'Review', assignee_role: 'editor', outcomes: ['approve', 'reject'] } },
        { id: 'end', type: 'end', config: {} },
      ],
      edges: [
        { id: 'e1', source: 'start', target: 'review' },
        { id: 'e2', source: 'review', target: 'end', outcome: 'approve' },
        { id: 'e3', source: 'review', target: 'end', outcome: 'reject' },
      ],
    }],
  }],
}

const noModel = { modules: [] }

function fakeClient(over: Record<string, unknown> = {}) {
  return { createProposal: vi.fn(async () => ({ id: 'p1', status: 'pending', approval_level: 'A1', diff: { changes: [], level: 'A1' } })), ...over } as unknown as ApiClient
}

const getModel = (client: ApiClient) => (client.createProposal as ReturnType<typeof vi.fn>).mock.calls[0]![1] as typeof model

describe('ProcessDesigner', () => {
  it('renders the process list', () => {
    render(<ProcessDesigner client={fakeClient()} applicationId="a1" model={model} canEdit={true} />)
    expect(screen.getByText(/Approval/)).toBeInTheDocument()
    expect(screen.getByText(/3 steps/)).toBeInTheDocument()
  })

  it('shows a hint when there are no modules', () => {
    render(<ProcessDesigner client={fakeClient()} applicationId="a1" model={noModel} canEdit={true} />)
    expect(screen.getByText(/No modules found/)).toBeInTheDocument()
  })

  it('creates a new process with default start/end steps', async () => {
    const client = fakeClient()
    render(<ProcessDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: 'New process' }))
    await userEvent.type(screen.getByLabelText('Key'), 'Onboarding')
    // fill start_roles
    await userEvent.clear(screen.getByLabelText('Start roles'))
    await userEvent.type(screen.getByLabelText('Start roles'), 'viewer')
    await userEvent.click(screen.getByRole('button', { name: 'Submit as proposal' }))
    expect(client.createProposal).toHaveBeenCalledOnce()
    const newMod = getModel(client).modules[0]!
    const added = (newMod.processes as { key: string; steps: unknown[] }[]).find((p) => p.key === 'Onboarding')!
    expect(added).toBeDefined()
    expect(added.steps).toHaveLength(2)
    expect(screen.getByText(/Proposal submitted/)).toBeInTheDocument()
  })

  it('prevents creating a duplicate process key', async () => {
    const client = fakeClient()
    render(<ProcessDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: 'New process' }))
    await userEvent.type(screen.getByLabelText('Key'), 'Approval')
    await userEvent.click(screen.getByRole('button', { name: 'Submit as proposal' }))
    expect(screen.getByRole('alert')).toHaveTextContent('already exists')
    expect(client.createProposal).not.toHaveBeenCalled()
  })

  it('opens a process for editing and shows step/edge tables', async () => {
    render(<ProcessDesigner client={fakeClient()} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: /^Approval/ }))
    expect(screen.getByRole('region', { name: /Edit process Approval/ })).toBeInTheDocument()
    expect(screen.getByText('review')).toBeInTheDocument()
    expect(screen.getByText('e1')).toBeInTheDocument()
  })

  it('adds a new user_task step and submits a proposal', async () => {
    const client = fakeClient()
    render(<ProcessDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: /^Approval/ }))

    // open "Add step" form
    const stepsSection = screen.getByText('Steps').closest('section')!
    await userEvent.click(within(stepsSection).getByRole('button', { name: 'Add step' }))

    const stepForm = screen.getByRole('form', { name: 'Add step' })
    await userEvent.clear(within(stepForm).getByLabelText('ID'))
    await userEvent.type(within(stepForm).getByLabelText('ID'), 'notify')
    // change type to system_task
    await userEvent.selectOptions(within(stepForm).getByLabelText('Type'), 'system_task')
    // fill required flow
    await userEvent.type(within(stepForm).getByPlaceholderText(/ReorderLowStock|select flow/), 'NotifyUser')
    await userEvent.click(within(stepForm).getByRole('button', { name: 'Add' }))

    // step is now in the table
    expect(screen.getByText('notify')).toBeInTheDocument()

    // submit the process
    await userEvent.click(screen.getByRole('button', { name: 'Submit as proposal' }))
    expect(client.createProposal).toHaveBeenCalledOnce()
    const procs = (getModel(client).modules[0] as { processes: { key: string; steps: { id: string }[] }[] }).processes
    const approval = procs.find((p) => p.key === 'Approval')!
    expect(approval.steps.map((s) => s.id)).toContain('notify')
  })

  it('prevents a duplicate step ID', async () => {
    const client = fakeClient()
    render(<ProcessDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: /^Approval/ }))
    const stepsSection = screen.getByText('Steps').closest('section')!
    await userEvent.click(within(stepsSection).getByRole('button', { name: 'Add step' }))
    const stepForm = screen.getByRole('form', { name: 'Add step' })
    await userEvent.clear(within(stepForm).getByLabelText('ID'))
    await userEvent.type(within(stepForm).getByLabelText('ID'), 'start')
    await userEvent.click(within(stepForm).getByRole('button', { name: 'Add' }))
    expect(screen.getByRole('alert')).toHaveTextContent('already exists')
    expect(client.createProposal).not.toHaveBeenCalled()
  })

  it('adds an edge and submits', async () => {
    const client = fakeClient()
    render(<ProcessDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: /^Approval/ }))
    const edgesSection = screen.getByText('Edges').closest('section')!
    await userEvent.click(within(edgesSection).getByRole('button', { name: 'Add edge' }))

    const edgeForm = screen.getByRole('form', { name: 'Add edge' })
    await userEvent.type(within(edgeForm).getByLabelText('ID'), 'e4')
    await userEvent.selectOptions(within(edgeForm).getByLabelText('Target'), 'start')
    await userEvent.click(within(edgeForm).getByRole('button', { name: 'Add' }))

    await userEvent.click(screen.getByRole('button', { name: 'Submit as proposal' }))
    expect(client.createProposal).toHaveBeenCalledOnce()
    const procs = (getModel(client).modules[0] as { processes: { key: string; edges: { id: string }[] }[] }).processes
    expect(procs.find((p) => p.key === 'Approval')!.edges.map((e) => e.id)).toContain('e4')
  })

  it('submits a delete proposal for a process', async () => {
    const client = fakeClient()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<ProcessDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: 'Delete process Approval' }))
    expect(client.createProposal).toHaveBeenCalledOnce()
    const procs = (getModel(client).modules[0] as { processes: { key: string }[] }).processes
    expect(procs.every((p) => p.key !== 'Approval')).toBe(true)
  })

  it('hides New/Delete/Add buttons when canEdit is false', () => {
    render(<ProcessDesigner client={fakeClient()} applicationId="a1" model={model} canEdit={false} />)
    expect(screen.queryByRole('button', { name: 'New process' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete process/ })).not.toBeInTheDocument()
  })
})
