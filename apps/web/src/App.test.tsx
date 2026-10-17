import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import App from './App'
import { ApiClient, ApiError } from './api/client'

function fakeClient(over: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    signedIn: false,
    login: vi.fn(async () => ({ id: '1', email: 'a@b.c', display_name: 'Ada' })),
    logout: vi.fn(async () => undefined),
    me: vi.fn(),
    organizations: vi.fn(async () => ({ items: [{ id: 'o1', name: 'Acme', slug: 'acme', role: 'editor' }], limit: 200, offset: 0, total: 1 })),
    projects: vi.fn(async () => ({ items: [], limit: 200, offset: 0, total: 0 })),
    ...over,
  } as unknown as ApiClient
}

describe('App', () => {
  it('signs in and lists organizations', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<App client={client} />)
    await user.type(screen.getByLabelText('Email'), 'a@b.c')
    await user.type(screen.getByLabelText('Password'), 'a long passphrase')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('Ada')).toBeInTheDocument()
    expect(client.login).toHaveBeenCalledWith('a@b.c', 'a long passphrase')
    expect(await screen.findByRole('option', { name: 'Acme' })).toBeInTheDocument()
  })

  it('shows the server’s message for a failed sign-in', async () => {
    const user = userEvent.setup()
    const client = fakeClient({ login: vi.fn(async () => { throw new ApiError(401, 'invalid_credentials', 'invalid email or password') }) })
    render(<App client={client} />)
    await user.type(screen.getByLabelText('Email'), 'a@b.c')
    await user.type(screen.getByLabelText('Password'), 'x')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('invalid email or password')
  })

  it('opens the demo flow without a server and goes back', async () => {
    const user = userEvent.setup()
    render(<App client={fakeClient()} />)
    await user.click(screen.getByRole('button', { name: 'Open the demo flow' }))
    expect(screen.getByRole('toolbar', { name: 'Flow actions' })).toHaveTextContent('ReorderLowStock')
    expect(screen.getByText(/Live validation is not connected/, { selector: '.validation' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save as proposal' })).toBeNull() // nowhere to save in the demo
    await user.click(screen.getByRole('button', { name: '← All flows' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: /Flow builder/ })).toBeInTheDocument())
  })
})
