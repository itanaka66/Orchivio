import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { viewOf } from '../flow/model'
import sampleModel from '../flow/sample-model.json'
import sample from '../flow/sample-flow.json'
import type { Flow, Issue } from '../flow/types'
import { FlowEditor, type FlowEditorProps } from './FlowEditor'

const flow = sample as unknown as Flow
const model = viewOf(sampleModel as never, 'core')

function setup(props: Partial<FlowEditorProps> = {}) {
  const user = userEvent.setup()
  const utils = render(<FlowEditor flow={flow} model={model} {...props} />)
  return { user, ...utils }
}

/** user-event's mouse sequence trips d3-drag inside React Flow in jsdom (no `view`); a plain click selects a node. */
const selectNode = (title: string | RegExp) => {
  const el = typeof title === 'string' ? screen.getByText(title) : nodeNamed(title)
  fireEvent.click(el.closest('.react-flow__node') ?? el)
}
// React Flow renders unmeasured nodes with visibility:hidden (jsdom never measures), and hidden elements
// get an empty accessible name, so nodes are found by their aria-label instead of by role+name.
const nodeNamed = (name: RegExp | string) => screen.getByLabelText(name)
const stepCount = () => document.querySelectorAll('.step').length

describe('FlowEditor', () => {
  it('shows one accessible node per step', () => {
    setup()
    expect(stepCount()).toBe(flow.steps.length)
    expect(nodeNamed(/^Find products, Retrieve step/)).toBeInTheDocument()
    expect(nodeNamed(/^Any found\?, Decision step/)).toBeInTheDocument()
    expect(screen.getByRole('status')).toBeInTheDocument() // announcements for assistive technology
  })

  it('adds a step from the palette and can undo and redo it', async () => {
    const { user } = setup()
    const before = stepCount()
    const undo = screen.getByRole('button', { name: 'Undo' })
    expect(undo).toBeDisabled()
    await user.click(screen.getByRole('button', { name: /^Add Retrieve step/ }))
    expect(stepCount()).toBe(before + 1)
    expect(screen.getByRole('status')).toHaveTextContent(/Added Retrieve step retrieve_1/)
    expect(screen.getByRole('region', { name: /Edit Retrieve/ })).toBeInTheDocument() // the new step is selected
    await user.click(undo)
    expect(stepCount()).toBe(before)
    await user.click(screen.getByRole('button', { name: 'Redo' }))
    expect(stepCount()).toBe(before + 1)
  })

  it('only offers steps a client flow may use', () => {
    setup({ flow: { ...flow, kind: 'client', steps: flow.steps.slice(0, 1), edges: [] } })
    const nav = screen.getByRole('navigation', { name: 'Add a step' })
    const labels = within(nav).getAllByRole('button').map((b) => b.getAttribute('aria-label') ?? '')
    expect(labels.map((l) => l.replace(/^Add (.*?) step.*/, '$1'))).toEqual(['Decision', 'Call flow', 'End'])
  })

  it('lets a keyboard user choose what happens next, with the same rules as dragging', async () => {
    const { user } = setup({ flow: { ...flow, edges: [] } })
    selectNode('Find products')
    const panel = screen.getByRole('region', { name: /Edit Find products/ })
    const next = within(panel).getByRole('combobox', { name: 'Next step' })
    await user.selectOptions(next, 'any')
    expect(screen.getByRole('status')).toHaveTextContent(/find \(out\) now continues to any/)
    expect(next).toHaveValue('any')
    await user.selectOptions(next, '')
    expect(screen.getByRole('status')).toHaveTextContent(/no longer continues/)
  })

  it('refuses a connection that would create a cycle and says why', async () => {
    const { user } = setup()
    selectNode('Any found?')
    const panel = screen.getByRole('region', { name: /Edit Any found/ })
    // nothing may connect into the start step, so it is not even offered
    expect(within(panel).queryByRole('option', { name: /Start/ })).toBeNull()
    // find -> any already exists, so any -> find would close a cycle
    await user.selectOptions(within(panel).getByRole('combobox', { name: /When “true”/ }), 'find')
    expect(screen.getByRole('status')).toHaveTextContent(/cycle/)
    expect(within(panel).getByRole('combobox', { name: /When “true”/ })).toHaveValue('each')
  })

  it('shows server validation on the node, in the problem list and blocks saving', async () => {
    const issues: Issue[] = [{
      severity: 'error', code: 'expression', message: 'expected bool, got int',
      path: '/modules/0/flows/ReorderLowStock/steps/any/config/expr',
    }]
    const validate = vi.fn().mockResolvedValue(issues)
    const onSave = vi.fn()
    setup({ validate, onSave })
    await waitFor(() => expect(nodeNamed(/^Any found\?, Decision step, 1 error/)).toBeInTheDocument(), { timeout: 3000 })
    const problems = screen.getByRole('region', { name: 'Problems' })
    expect(within(problems).getByText(/expected bool, got int/)).toBeInTheDocument()
    expect(screen.getByText('1 error to fix.', { selector: '.validation' })).toBeInTheDocument()

    selectNode('Any found?')
    const field = screen.getByLabelText(/Condition/)
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: 'Save as proposal' })).toBeDisabled()
  })

  it('validates again after an edit, and saves the edited flow', async () => {
    const validate = vi.fn().mockResolvedValue([])
    const onSave = vi.fn().mockResolvedValue('Proposal created.')
    const { user } = setup({ validate, onSave })
    await waitFor(() => expect(screen.getByText('No problems found.', { selector: '.validation' })).toBeInTheDocument(), { timeout: 3000 })
    const save = screen.getByRole('button', { name: 'Save as proposal' })
    expect(save).toBeDisabled() // nothing changed yet

    selectNode('Any found?')
    const cond = screen.getByLabelText(/Condition/)
    await user.clear(cond)
    await user.type(cond, 'size(low) > 1')
    await waitFor(() => expect(validate).toHaveBeenCalledTimes(2), { timeout: 3000 })
    await waitFor(() => expect(save).toBeEnabled(), { timeout: 3000 })
    await user.click(save)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Proposal created.'))
    const saved = onSave.mock.calls[0]?.[0] as Flow
    expect(saved.steps.find((s) => s.id === 'any')?.config.expr).toBe('size(low) > 1')
    expect(save).toBeDisabled() // saved, so no longer dirty
  })

  it('collapses typing into one undo step', async () => {
    const { user } = setup()
    selectNode('Any found?')
    const cond = screen.getByLabelText(/Condition/)
    await user.clear(cond)
    await user.type(cond, 'abc')
    expect(cond).toHaveValue('abc')
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(screen.getByLabelText(/Condition/)).toHaveValue('size(low) > 0')
  })

  it('keeps working when validation is unavailable', async () => {
    const validate = vi.fn().mockRejectedValue(new Error('offline'))
    setup({ validate, onSave: vi.fn() })
    await waitFor(() => expect(screen.getByText(/Validation is unavailable/, { selector: '.validation' })).toBeInTheDocument(), { timeout: 3000 })
  })

  it('removes the connections of a deleted step and never deletes the start step', async () => {
    const { user } = setup()
    selectNode('Find products')
    await user.click(screen.getByRole('button', { name: 'Delete this step' }))
    expect(stepCount()).toBe(flow.steps.length - 1)
    selectNode(/^Start/)
    expect(screen.queryByRole('button', { name: 'Delete this step' })).toBeNull()
  })

  it('is read-only when asked', () => {
    setup({ readOnly: true })
    expect(screen.getByRole('button', { name: /^Add Retrieve step/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Auto-arrange' })).toBeDisabled()
  })

  it('edits flow parameters when nothing is selected', async () => {
    const { user } = setup()
    const settings = screen.getByRole('region', { name: 'Flow settings' })
    expect(within(settings).getByLabelText('Parameter 1 name')).toHaveValue('min_gap')
    await user.click(within(settings).getByRole('button', { name: 'Add parameter' }))
    expect(within(settings).getByLabelText('Parameter 2 name')).toHaveValue('param_2')
  })

  it('edits field values as expression rows, suggesting the entity fields', async () => {
    const { user } = setup()
    selectNode(/^Change, Change step/)
    const panel = screen.getByRole('region', { name: /Edit Change/ })
    expect(within(panel).getByLabelText('Field values: field name 1')).toHaveValue('reorder_level')
    await user.click(within(panel).getByRole('button', { name: 'Add field' }))
    expect(within(panel).getByLabelText('Field values: field name 2')).toBeInTheDocument()
    const options = Array.from(panel.querySelectorAll('datalist option')).map((o) => o.getAttribute('value'))
    expect(options).toEqual(expect.arrayContaining(['name', 'sku', 'quantity']))
    expect(options).not.toContain('id') // system-managed fields are not suggested
  })
})

describe('FlowEditor run panel', () => {
  const result = {
    flow: 'core.ReorderLowStock', status: 'succeeded' as const, error: null, error_step: null, steps_executed: 4,
    trace: [
      { flow: 'core.ReorderLowStock', step: 'start', type: 'start', status: 'ok' as const, ms: 0, iteration: [], detail: '' },
      { flow: 'core.ReorderLowStock', step: 'find', type: 'retrieve', status: 'ok' as const, ms: 1, iteration: [], detail: '2 record(s)' },
      { flow: 'core.ReorderLowStock', step: 'bump', type: 'change', status: 'ok' as const, ms: 1, iteration: [0], detail: 'updated core.Product' },
      { flow: 'core.ReorderLowStock', step: 'bump', type: 'change', status: 'ok' as const, ms: 1, iteration: [1], detail: 'updated core.Product' },
    ],
  }

  it('runs the saved flow with parsed arguments and shows the trace on the canvas', async () => {
    const run = vi.fn().mockResolvedValue(result)
    const { user } = setup({ run })
    const panel = screen.getByRole('region', { name: 'Run' })
    await user.type(within(panel).getByLabelText(/min_gap/), '3')
    await user.click(within(panel).getByRole('button', { name: 'Run' }))
    await waitFor(() => expect(within(panel).getByText(/Succeeded after 4 steps/)).toBeInTheDocument())
    expect(run).toHaveBeenCalledWith({ min_gap: 3 }, 'preview-user', ['viewer', 'editor', 'admin'])
    expect(within(panel).getByRole('list', { name: 'Steps that ran' }).children).toHaveLength(4)
    await waitFor(() => expect(nodeNamed(/^Change, Change step, ran 2 times in the last run/)).toBeInTheDocument())
    expect(nodeNamed(/^Done, End step$/)).toBeInTheDocument() // did not run
  })

  it('shows a failure and where it happened', async () => {
    const failed = { ...result, status: 'failed' as const, error: 'No product is below its reorder level', error_step: 'find',
      trace: [{ ...result.trace[1]!, status: 'error' as const }] }
    const { user } = setup({ run: vi.fn().mockResolvedValue(failed) })
    await user.click(screen.getByRole('button', { name: 'Run' }))
    expect((await screen.findAllByText(/No product is below its reorder level/)).length).toBeGreaterThan(0)
    await waitFor(() => expect(nodeNamed(/^Find products, Retrieve step, failed in the last run/)).toBeInTheDocument())
  })

  it('refuses to run unsaved edits, because runs use the approved model', async () => {
    const { user } = setup({ run: vi.fn(), onSave: vi.fn() })
    expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /^Add Decision step/ }))
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
    expect(screen.getByRole('note')).toHaveTextContent(/approved model/)
  })

  it('is hidden for client flows and blocked for flows that are not saved yet', () => {
    const { unmount } = setup({ run: vi.fn(), runnable: false })
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
    unmount()
    setup({ run: vi.fn(), flow: { ...flow, kind: 'client', steps: flow.steps.slice(0, 1), edges: [] } })
    expect(screen.queryByRole('region', { name: 'Run' })).toBeNull()
  })

  it('clears the trace when the flow is edited', async () => {
    const { user } = setup({ run: vi.fn().mockResolvedValue(result) })
    await user.click(screen.getByRole('button', { name: 'Run' }))
    await screen.findByText(/Succeeded after 4 steps/)
    await user.click(screen.getByRole('button', { name: /^Add Decision step/ }))
    expect(screen.queryByText(/Succeeded after 4 steps/)).toBeNull()
  })
})
