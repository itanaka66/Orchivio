import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type ApiClient } from '../api/client'
import { editable, Preview, resolvePages } from './Preview'

const model = {
  security: { roles: ['viewer', 'editor'] },
  modules: [{
    key: 'core',
    enumerations: [{ name: 'Kind', values: ['a', 'b'] }],
    entities: [{
      name: 'Product', display_name: 'Product', element_id: 'ent-1',
      fields: [
        { name: 'id', type: 'uuid', primary_key: true, generated: true, element_id: 'f-id' },
        { name: 'name', type: 'string', required: true, max_length: 50, element_id: 'f-name' },
        { name: 'quantity', type: 'integer', element_id: 'f-qty' },
        { name: 'kind', type: 'enum:Kind', element_id: 'f-kind' },
        { name: 'label', type: 'string', computed: { mode: 'virtual' }, element_id: 'f-label' },
      ],
    }],
    pages: [
      { key: 'product-list', path: '/products', type: 'list', entity: 'Product', element_id: 'page-1', columns: ['name', 'quantity'] },
      { key: 'product-create', path: '/products/new', type: 'form', entity: 'Product', mode: 'create', element_id: 'page-2' },
      { key: 'product-edit', path: '/products/edit', type: 'form', entity: 'Product', mode: 'edit', element_id: 'page-3' },
      { key: 'ghost', path: '/g', type: 'list', entity: 'Missing' },
    ],
  }],
}

function fake(over: Record<string, unknown> = {}) {
  return {
    previewList: vi.fn(async () => ({ items: [{ id: 'r1', name: 'Bolt', quantity: 3 }], total: 1 })),
    previewCreate: vi.fn(async () => ({})),
    previewUpdate: vi.fn(async () => ({})),
    previewReset: vi.fn(async () => ({})),
    submitFeedback: vi.fn(async () => ({})),
    ...over,
  }
}
const as = (c: unknown) => c as ApiClient
const show = (c: ReturnType<typeof fake>, canEdit = true) =>
  render(<Preview client={as(c)} applicationId="a1" model={model} roles={['viewer', 'editor']} canEdit={canEdit} />)

describe('resolvePages / editable', () => {
  it('keeps only pages whose entity exists and offers only typed-in fields', () => {
    const pages = resolvePages(model)
    expect(pages.map((p) => p.page.key)).toEqual(['product-list', 'product-create', 'product-edit'])
    expect(editable(pages[0]!.entity).map((f) => f.name)).toEqual(['name', 'quantity', 'kind'])
  })
})

describe('Preview', () => {
  it('lists records as the chosen user and marks the page, table and columns for feedback', async () => {
    const c = fake()
    show(c)
    expect(await screen.findByText('Bolt')).toBeInTheDocument()
    expect(c.previewList).toHaveBeenCalledWith('a1', 'core.Product', { user: 'preview-user', roles: ['viewer'] })
    const page = screen.getByRole('region', { name: 'Page product-list' })
    expect(page).toHaveAttribute('data-orchivio-page', 'product-list')
    expect(page).toHaveAttribute('data-orchivio-element', 'page-1')
    expect(screen.getByRole('columnheader', { name: 'name' })).toHaveAttribute('data-orchivio-element', 'f-name')
    expect(screen.getByRole('table')).toHaveAttribute('data-orchivio-element', 'ent-1')
  })

  it('reloads when the simulated roles change', async () => {
    const c = fake()
    show(c)
    await screen.findByText('Bolt')
    const roles = screen.getByLabelText('Preview roles')
    await userEvent.clear(roles)
    await userEvent.type(roles, 'editor')
    await waitFor(() => expect(c.previewList).toHaveBeenLastCalledWith('a1', 'core.Product', { user: 'preview-user', roles: ['editor'] }))
  })

  it('creates a record with numbers sent as numbers and empty fields left out', async () => {
    const c = fake()
    show(c)
    await userEvent.click(await screen.findByRole('button', { name: 'New Product' }))
    await userEvent.type(screen.getByLabelText(/^name/), 'Nut')
    await userEvent.type(screen.getByLabelText(/^quantity/), '7')
    await userEvent.selectOptions(screen.getByLabelText(/^kind/), 'b')
    expect(screen.queryByLabelText(/^label/)).toBeNull() // computed
    expect(screen.queryByLabelText(/^id/)).toBeNull() // generated key
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(c.previewCreate).toHaveBeenCalledWith('a1', 'core.Product', { name: 'Nut', quantity: 7, kind: 'b' }, { user: 'preview-user', roles: ['viewer'] })
    expect(await screen.findByRole('status')).toHaveTextContent('Saved.')
  })

  it('edits a row, and shows the server’s refusal', async () => {
    const c = fake({ previewUpdate: vi.fn().mockRejectedValue(new ApiError(403, 'forbidden', 'your roles may not update Product')) })
    show(c)
    await userEvent.click(await screen.findByRole('button', { name: 'Edit row 1' }))
    expect(screen.getByLabelText(/^name/)).toHaveValue('Bolt')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(c.previewUpdate).toHaveBeenCalledWith('a1', 'core.Product', 'r1', { name: 'Bolt', quantity: 3 }, expect.anything())
    expect(await screen.findByRole('alert')).toHaveTextContent('your roles may not update Product')
  })

  it('is read-only for people who cannot edit', async () => {
    show(fake(), false)
    await screen.findByText('Bolt')
    expect(screen.queryByRole('button', { name: 'New Product' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Edit row/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset sample data' })).toBeNull()
  })

  it('resets the preview data', async () => {
    const c = fake()
    show(c)
    await userEvent.click(await screen.findByRole('button', { name: 'Reset sample data' }))
    expect(c.previewReset).toHaveBeenCalledWith('a1')
    expect(await screen.findByRole('status')).toHaveTextContent('reset')
  })

  it('sends feedback about the element the person picks, with the page it is on', async () => {
    const c = fake()
    show(c)
    await screen.findByText('Bolt')
    await userEvent.click(screen.getByRole('button', { name: 'Feedback' }))
    await userEvent.type(screen.getByLabelText('What should change?'), 'Quantity should be bold')
    await userEvent.click(screen.getByRole('button', { name: 'Pick an element' }))
    await userEvent.click(within(screen.getByRole('table')).getByText('3'))
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(c.submitFeedback).toHaveBeenCalledWith('a1', { page_key: 'product-list', body: 'Quantity should be bold', element_id: 'f-qty', screenshot_base64: null })
  })

  it('says so when the model has no pages', () => {
    render(<Preview client={as(fake())} applicationId="a1" model={{ modules: [] }} roles={[]} canEdit />)
    expect(screen.getByText('The model has no pages yet.')).toBeInTheDocument()
  })
})
