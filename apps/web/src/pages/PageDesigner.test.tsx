import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ApiClient } from '../api/client'
import { PageDesigner } from './PageDesigner'

const model = {
  modules: [{
    key: 'core',
    entities: [
      {
        name: 'Product', display_name: 'Product',
        fields: [
          { name: 'id', type: 'uuid', primary_key: true, generated: true },
          { name: 'name', type: 'string' },
          { name: 'qty', type: 'integer' },
        ],
      },
    ],
    pages: [
      { key: 'product-list', path: '/products', type: 'list', entity: 'Product', columns: ['name', 'qty'], actions: ['create', 'edit'] },
      { key: 'product-form', path: '/products/new', type: 'form', entity: 'Product', mode: 'create' },
    ],
  }],
}

const noModel = { modules: [] }

function fakeClient(over: Record<string, unknown> = {}) {
  return { createProposal: vi.fn(async () => ({ id: 'p1', status: 'pending', approval_level: 'A1', diff: { changes: [], level: 'A1' } })), ...over } as unknown as ApiClient
}

describe('PageDesigner', () => {
  it('renders the page list', () => {
    render(<PageDesigner client={fakeClient()} applicationId="a1" model={model} canEdit={true} />)
    expect(screen.getByText('product-list')).toBeInTheDocument()
    expect(screen.getByText('product-form')).toBeInTheDocument()
  })

  it('shows a hint when there are no modules', () => {
    render(<PageDesigner client={fakeClient()} applicationId="a1" model={noModel} canEdit={true} />)
    expect(screen.getByText(/No modules found/)).toBeInTheDocument()
  })

  it('opens a new-page form and submits a create proposal', async () => {
    const client = fakeClient()
    render(<PageDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: 'New page' }))
    expect(screen.getByRole('region', { name: 'New page' })).toBeInTheDocument()

    await userEvent.clear(screen.getByLabelText('Key'))
    await userEvent.type(screen.getByLabelText('Key'), 'product-detail')
    await userEvent.clear(screen.getByLabelText('Path'))
    await userEvent.type(screen.getByLabelText('Path'), '/products/detail')

    await userEvent.click(screen.getByRole('button', { name: 'Submit as proposal' }))
    expect(client.createProposal).toHaveBeenCalledOnce()
    const [, newModel, title] = (client.createProposal as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(title).toBe('Add page product-detail')
    const pages = (newModel as { modules: { pages: { key: string }[] }[] }).modules[0]!.pages
    expect(pages).toHaveLength(3)
    expect(pages[2]!.key).toBe('product-detail')
    expect(screen.getByText(/Proposal submitted/)).toBeInTheDocument()
  })

  it('validates that key is unique', async () => {
    const client = fakeClient()
    render(<PageDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: 'New page' }))
    await userEvent.clear(screen.getByLabelText('Key'))
    await userEvent.type(screen.getByLabelText('Key'), 'product-list')
    await userEvent.clear(screen.getByLabelText('Path'))
    await userEvent.type(screen.getByLabelText('Path'), '/p')
    await userEvent.click(screen.getByRole('button', { name: 'Submit as proposal' }))
    expect(screen.getByRole('alert')).toHaveTextContent('already exists')
    expect(client.createProposal).not.toHaveBeenCalled()
  })

  it('opens an existing page for editing and shows columns/actions', async () => {
    render(<PageDesigner client={fakeClient()} applicationId="a1" model={model} canEdit={true} />)
    // use type prefix to distinguish the edit button from the "Delete page product-list" button
    await userEvent.click(screen.getByRole('button', { name: /^list product-list/ }))
    const panel = screen.getByRole('region', { name: /Edit page product-list/ })
    expect(within(panel).getByText(/^Columns/)).toBeInTheDocument()
    expect(within(panel).getByText(/^Actions/)).toBeInTheDocument()
    expect(within(panel).getByLabelText(/\bname\b/)).toBeChecked()
    expect(within(panel).getByLabelText(/\bqty\b/)).toBeChecked()
    expect(within(panel).getByLabelText(/\bcreate\b/)).toBeChecked()
    expect(within(panel).getByLabelText(/\bdelete\b/)).not.toBeChecked()
  })

  it('submits an edit proposal with updated columns', async () => {
    const client = fakeClient()
    render(<PageDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: /^list product-list/ }))
    // uncheck 'qty' column
    const qtyBox = screen.getByLabelText(/\bqty\b/)
    await userEvent.click(qtyBox)
    await userEvent.click(screen.getByRole('button', { name: 'Submit as proposal' }))
    expect(client.createProposal).toHaveBeenCalledOnce()
    const [, newModel, title] = (client.createProposal as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(title).toBe('Edit page product-list')
    const pages = (newModel as { modules: { pages: { key: string; columns: string[] }[] }[] }).modules[0]!.pages
    const edited = pages.find((p) => p.key === 'product-list')!
    expect(edited.columns).toEqual(['name'])
  })

  it('submits a delete proposal after confirmation', async () => {
    const client = fakeClient()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<PageDesigner client={client} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: 'Delete page product-form' }))
    expect(client.createProposal).toHaveBeenCalledOnce()
    const [, newModel, title] = (client.createProposal as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(title).toBe('Delete page product-form')
    const pages = (newModel as { modules: { pages: { key: string }[] }[] }).modules[0]!.pages
    expect(pages.every((p) => p.key !== 'product-form')).toBe(true)
  })

  it('does not show New/Delete buttons when canEdit is false', () => {
    render(<PageDesigner client={fakeClient()} applicationId="a1" model={model} canEdit={false} />)
    expect(screen.queryByRole('button', { name: 'New page' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete page/ })).not.toBeInTheDocument()
  })

  it('does not show form-type pages in the columns section', async () => {
    render(<PageDesigner client={fakeClient()} applicationId="a1" model={model} canEdit={true} />)
    await userEvent.click(screen.getByRole('button', { name: /^form product-form/ }))
    const panel = screen.getByRole('region', { name: /Edit page product-form/ })
    expect(within(panel).queryByText('Columns')).not.toBeInTheDocument()
    expect(within(panel).queryByText('Actions')).not.toBeInTheDocument()
  })
})
