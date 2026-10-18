import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type ApiClient, type FeedbackItem, type Requirement, type Story } from '../api/client'
import { Board } from './Board'
import { collectElements } from './elements'
import { FeedbackWidget, MAX_SCREENSHOT_BYTES, targetOf, toBase64 } from './FeedbackWidget'

const story = (over: Partial<Story> = {}): Story => ({ id: 's1', title: 'Search products', body: '', acceptance: '', status: 'backlog', source: 'manual', position: 1, requirement_id: null, ...over })
const requirement = (over: Partial<Requirement> = {}): Requirement => ({ id: 'r1', title: 'Find items', body: '', status: 'draft', source: 'manual', ...over })
const feedbackItem = (over: Partial<FeedbackItem> = {}): FeedbackItem => ({ id: 'f1', page_key: 'core.Products', element_id: null, body: 'Needs a search box', status: 'new', has_screenshot: false, story_id: null, created_at: '', ...over })

function fake(over: Record<string, unknown> = {}) {
  return {
    stories: vi.fn(async () => ({ items: [story()] })),
    requirements: vi.fn(async () => ({ items: [requirement()] })),
    createStory: vi.fn(async () => story()),
    updateStory: vi.fn(async () => story()),
    deleteStory: vi.fn(async () => undefined),
    createRequirement: vi.fn(async () => requirement()),
    updateRequirement: vi.fn(async () => requirement()),
    deleteRequirement: vi.fn(async () => undefined),
    extractRequirements: vi.fn(async () => ({ requirements: [requirement(), requirement({ id: 'r2' })], skipped_duplicates: 1 })),
    feedback: vi.fn(async () => ({ items: [feedbackItem()] })),
    dismissFeedback: vi.fn(async () => feedbackItem({ status: 'dismissed' })),
    clusterFeedback: vi.fn(async () => ({ stories: [story({ id: 's9', status: 'proposed' })], unclustered: 0 })),
    screenshotUrl: vi.fn(() => '/shot'),
    traceability: vi.fn(async () => ({
      requirements: [{ id: 'r1', title: 'Find items', status: 'draft', stories: [], elements: [{ id: 'e1', name: 'entity Product', exists: true }], tests: [{ id: 't', title: null, exists: false }], revisions: [] }],
      stories: [],
      coverage: { requirements: 1, with_stories: 0, with_elements: 1, with_tests: 1, with_revisions: 0, dangling_elements: 0, dangling_tests: 1 },
    })),
    traceLinks: vi.fn(async () => ({ items: [{ id: 'l1', from_type: 'requirement', from_id: 'r1', to_type: 'element', to_id: 'e1' }] })),
    createTraceLink: vi.fn(async () => ({})),
    deleteTraceLink: vi.fn(async () => undefined),
    testCases: vi.fn(async () => ({ items: [{ id: 'crud:core.Product', title: 'create, read, update, delete one Product' }], total: 1 })),
    ...over,
  }
}
const as = (c: unknown) => c as ApiClient
const elements = [{ id: 'e1', label: 'entity core.Product' }]

describe('story board', () => {
  it('groups stories into columns and moves one with the keyboard-accessible select', async () => {
    const c = fake()
    render(<Board client={as(c)} projectId="p1" applicationId="a1" elements={elements} canEdit />)
    const backlog = await screen.findByRole('region', { name: 'Backlog' })
    expect(within(backlog).getByText('Search products')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Done' })).queryByText('Search products')).toBeNull()
    await userEvent.selectOptions(within(backlog).getByLabelText('Move “Search products” to'), 'done')
    expect(c.updateStory).toHaveBeenCalledWith('p1', 's1', { status: 'done', position: 2 })
  })

  it('moves a story by dragging it onto a column', async () => {
    const c = fake()
    render(<Board client={as(c)} projectId="p1" elements={[]} canEdit />)
    const card = (await screen.findByText('Search products')).closest('li')!
    const dataTransfer = { data: {} as Record<string, string>, setData(k: string, v: string) { this.data[k] = v }, getData(k: string) { return this.data[k] ?? '' } }
    fireEvent.dragStart(card, { dataTransfer })
    fireEvent.drop(screen.getByRole('region', { name: 'In progress' }), { dataTransfer })
    await waitFor(() => expect(c.updateStory).toHaveBeenCalledWith('p1', 's1', expect.objectContaining({ status: 'in_progress' })))
  })

  it('is read-only for people who cannot edit', async () => {
    render(<Board client={as(fake())} projectId="p1" elements={[]} canEdit={false} />)
    await screen.findByText('Search products')
    expect(screen.queryByLabelText('New story')).toBeNull()
    expect(screen.queryByLabelText('Move “Search products” to')).toBeNull()
    expect(screen.getByText('Search products').closest('li')).toHaveAttribute('draggable', 'false')
  })

  it('adds a story and shows the server’s error when it fails', async () => {
    const c = fake({ createStory: vi.fn().mockRejectedValueOnce(new ApiError(403, 'forbidden', 'your role does not allow this action')) })
    render(<Board client={as(c)} projectId="p1" elements={[]} canEdit />)
    await userEvent.type(await screen.findByLabelText('New story'), 'Export CSV')
    await userEvent.click(screen.getByRole('button', { name: 'Add story' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('your role does not allow this action')
    expect(c.createStory).toHaveBeenCalledWith('p1', 'Export CSV')
  })

  it('marks AI-created stories and requirements so people review them', async () => {
    const c = fake({ stories: vi.fn(async () => ({ items: [story({ status: 'proposed', source: 'feedback' })] })), requirements: vi.fn(async () => ({ items: [requirement({ source: 'ai' })] })) })
    render(<Board client={as(c)} projectId="p1" elements={[]} canEdit />)
    expect(within(await screen.findByRole('region', { name: 'Proposed' })).getByText('feedback')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Requirements' }))
    expect(screen.getByText('AI draft')).toBeInTheDocument()
  })

  it('extracts requirements from a pasted document and reports duplicates', async () => {
    const c = fake()
    render(<Board client={as(c)} projectId="p1" applicationId="a1" elements={[]} canEdit />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Requirements' }))
    await userEvent.type(screen.getByLabelText('Document'), 'We need search.')
    await userEvent.click(screen.getByRole('button', { name: 'Extract requirements' }))
    expect(c.extractRequirements).toHaveBeenCalledWith('a1', 'We need search.')
    expect(await screen.findByRole('status')).toHaveTextContent('2 draft requirements added, 1 duplicate skipped.')
  })

  it('offers no extraction without an application', async () => {
    render(<Board client={as(fake())} projectId="p1" elements={[]} canEdit />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Requirements' }))
    expect(screen.queryByLabelText('Document')).toBeNull()
  })

  it('reviews feedback: dismiss and group into proposed stories', async () => {
    const c = fake()
    render(<Board client={as(c)} projectId="p1" applicationId="a1" elements={[]} canEdit />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Feedback' }))
    expect(await screen.findByText('Needs a search box')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Group into proposed stories' }))
    expect(await screen.findByRole('status')).toHaveTextContent('1 story proposal created; 0 items left over.')
    await userEvent.click(screen.getByRole('button', { name: /Dismiss feedback/ }))
    expect(c.dismissFeedback).toHaveBeenCalledWith('a1', 'f1')
  })

  it('shows coverage, broken links and lets editors add and remove links', async () => {
    const c = fake()
    render(<Board client={as(c)} projectId="p1" elements={elements} canEdit />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Traceability' }))
    const cov = await screen.findByRole('group', { name: 'Coverage' }).catch(() => screen.getByLabelText('Coverage'))
    expect(cov).toHaveTextContent('Broken links1')
    expect(screen.getByText('missing test')).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText('From'), 'requirement:r1')
    await userEvent.selectOptions(screen.getByLabelText('Target'), 'e1')
    await userEvent.click(screen.getByRole('button', { name: 'Link' }))
    expect(c.createTraceLink).toHaveBeenCalledWith('p1', { from_type: 'requirement', from_id: 'r1', to_type: 'element', to_id: 'e1' })
    await userEvent.click(screen.getByRole('button', { name: /Remove link/ }))
    expect(c.deleteTraceLink).toHaveBeenCalledWith('p1', 'l1')
  })

  it('links to generated test cases chosen from the catalogue', async () => {
    const c = fake()
    render(<Board client={as(c)} projectId="p1" elements={[]} canEdit />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Traceability' }))
    await userEvent.selectOptions(await screen.findByLabelText('From'), 'story:s1')
    await userEvent.selectOptions(screen.getByLabelText('To'), 'test')
    await userEvent.selectOptions(await screen.findByRole('option', { name: /create, read, update/ }).then((o) => o.closest('select')!), 'crud:core.Product')
    await userEvent.click(screen.getByRole('button', { name: 'Link' }))
    expect(c.createTraceLink).toHaveBeenCalledWith('p1', { from_type: 'story', from_id: 's1', to_type: 'test', to_id: 'crud:core.Product' })
  })
})

describe('collectElements', () => {
  it('labels elements by kind and path and ignores duplicate ids', () => {
    const model = { modules: [{ key: 'core', element_id: 'm', entities: [{ name: 'Product', element_id: 'e1', fields: [{ name: 'sku', element_id: 'f1' }, { name: 'dup', element_id: 'e1' }] }] }] }
    expect(collectElements(model)).toEqual([
      { id: 'm', label: 'module core' },
      { id: 'e1', label: 'entity core.Product' },
      { id: 'f1', label: 'field core.Product.sku' },
    ])
  })
})

describe('feedback widget', () => {
  const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0])

  it('reads element and page from data attributes', () => {
    document.body.innerHTML = '<section data-orchivio-page="core.Products"><div data-orchivio-element="e-1"><span id="x">hi</span></div></section><p id="y">no</p>'
    expect(targetOf(document.getElementById('x'))).toEqual({ element: 'e-1', page: 'core.Products' })
    expect(targetOf(document.getElementById('y'))).toEqual({ element: null, page: null })
    document.body.innerHTML = ''
  })

  it('encodes bytes as base64 in chunks', () => {
    expect(toBase64(new Uint8Array([104, 105]))).toBe('aGk=')
    expect(atob(toBase64(new Uint8Array(100_000).fill(65))).length).toBe(100_000)
  })

  it('sends text, the picked element and an attached image', async () => {
    const submit = vi.fn(async () => feedbackItem())
    render(
      <>
        <section data-orchivio-page="core.Products"><button type="button" data-orchivio-element="e-1">Buy</button></section>
        <FeedbackWidget client={{ submitFeedback: submit }} applicationId="a1" page="studio" />
      </>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Feedback' }))
    await userEvent.type(screen.getByLabelText('What should change?'), 'Too small')
    await userEvent.click(screen.getByRole('button', { name: 'Pick an element' }))
    expect(screen.getByRole('status')).toHaveTextContent('Click the part of the page')
    const target = screen.getByRole('button', { name: 'Buy' })
    const clicked = vi.fn()
    target.addEventListener('click', clicked)
    await userEvent.click(target)
    expect(clicked).not.toHaveBeenCalled() // picking does not trigger the page's own handlers
    await userEvent.upload(screen.getByLabelText('Attach an image'), new File([PNG], 's.png', { type: 'image/png' }))
    expect(await screen.findByText(/Image attached/)).toBeInTheDocument()
    expect(screen.getByLabelText('What should change?')).toHaveValue('Too small')
    expect(screen.getByText('core.Products')).toBeInTheDocument() // page key came from the clicked element's page
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(submit).toHaveBeenCalledWith('a1', { page_key: 'core.Products', body: 'Too small', element_id: 'e-1', screenshot_base64: toBase64(PNG) })
    expect(await screen.findByText('Thanks, your feedback was sent.')).toBeInTheDocument()
  })

  it('refuses files that are not PNG or JPEG, or too large, before sending anything', async () => {
    const submit = vi.fn()
    render(<FeedbackWidget client={{ submitFeedback: submit }} applicationId="a1" page="studio" />)
    await userEvent.click(screen.getByRole('button', { name: 'Feedback' }))
    await userEvent.upload(screen.getByLabelText('Attach an image'), new File([new TextEncoder().encode('<svg onload=alert(1)>')], 'x.png', { type: 'image/png' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Only PNG or JPEG')
    const big = new Uint8Array(MAX_SCREENSHOT_BYTES + 1)
    big.set(PNG)
    await userEvent.upload(screen.getByLabelText('Attach an image'), new File([big], 'big.png', { type: 'image/png' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('larger than 512 KiB')
    expect(submit).not.toHaveBeenCalled()
  })

  it('shows the server’s message and keeps the text when sending fails', async () => {
    const submit = vi.fn().mockRejectedValue(new ApiError(429, 'too_much_feedback', 'at most 30 feedback items per hour'))
    render(<FeedbackWidget client={{ submitFeedback: submit }} applicationId="a1" page="studio:board" />)
    await userEvent.click(screen.getByRole('button', { name: 'Feedback' }))
    await userEvent.type(screen.getByLabelText('What should change?'), 'Hello')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('at most 30 feedback items per hour')
    expect(screen.getByLabelText('What should change?')).toHaveValue('Hello')
    expect(submit).toHaveBeenCalledWith('a1', { page_key: 'studio:board', body: 'Hello', element_id: null, screenshot_base64: null })
  })

  it('cancelling picking with Escape returns to the form', async () => {
    render(<FeedbackWidget client={{ submitFeedback: vi.fn() }} applicationId="a1" page="studio" />)
    await userEvent.click(screen.getByRole('button', { name: 'Feedback' }))
    await userEvent.click(screen.getByRole('button', { name: 'Pick an element' }))
    await userEvent.keyboard('{Escape}')
    expect(await screen.findByRole('dialog', { name: 'Send feedback' })).toBeInTheDocument()
  })
})
