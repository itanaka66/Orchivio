// Thin client for the Orchivio API (docs/API-SPEC.md). Cookie session + CSRF header.
import type { Flow, Issue, RunResult } from '../flow/types'

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string, readonly details?: unknown) {
    super(message)
    this.name = 'ApiError'
  }
}

export interface User { id: string; email: string; display_name: string }
export interface Page<T> { items: T[]; limit: number; offset: number; total: number }
export interface Org { id: string; name: string; slug: string; role: string }
export interface Project { id: string; organization_id: string; name: string; slug: string }
export interface Application { id: string; project_id: string; key: string; name: string; current_revision_id: string | null; current_revision_no: number | null }
export interface Revision { id: string; revision_no: number; model_json: Record<string, unknown> }
export interface ProposalSummary { id: string; status: string; approval_level: string; diff: { changes: unknown[]; level: string } }

const CSRF_KEY = 'orchivio.csrf'

export class ApiClient {
  private csrf: string | null

  constructor(
    private readonly base = '/api/v1',
    private readonly doFetch: typeof fetch = (...args) => fetch(...args),
    private readonly storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null =
      typeof sessionStorage === 'undefined' ? null : sessionStorage,
  ) {
    this.csrf = storage?.getItem(CSRF_KEY) ?? null
  }

  get signedIn(): boolean { return this.csrf !== null }

  async request<T>(method: string, path: string, body?: unknown, extraHeaders: Record<string, string> = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json', ...extraHeaders }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    // Cookie-authenticated unsafe requests must carry the CSRF token returned at login.
    if (method !== 'GET' && this.csrf) headers['X-CSRF-Token'] = this.csrf
    const res = await this.doFetch(`${this.base}${path}`, {
      method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (res.status === 204) return undefined as T
    const text = await res.text()
    let data: unknown = undefined
    try { data = text ? JSON.parse(text) : undefined } catch { /* non-JSON error pages */ }
    if (!res.ok) {
      const e = (data as { error?: { code?: string; message?: string; details?: unknown } } | undefined)?.error
      if (res.status === 401) this.forget()
      throw new ApiError(res.status, e?.code ?? 'http_error', e?.message ?? `Request failed (${res.status})`, e?.details)
    }
    return data as T
  }

  private forget() {
    this.csrf = null
    this.storage?.removeItem(CSRF_KEY)
  }

  async login(email: string, password: string): Promise<User> {
    const r = await this.request<{ user: User; csrf_token: string }>('POST', '/auth/login', { email, password })
    this.csrf = r.csrf_token
    this.storage?.setItem(CSRF_KEY, r.csrf_token)
    return r.user
  }

  async logout(): Promise<void> {
    try { await this.request('POST', '/auth/logout') } finally { this.forget() }
  }

  me = () => this.request<{ user: User }>('GET', '/me')
  organizations = () => this.request<Page<Org>>('GET', '/organizations?limit=200')
  projects = (orgId: string) => this.request<Page<Project>>('GET', `/organizations/${orgId}/projects?limit=200`)
  applications = (projectId: string) => this.request<Page<Application>>('GET', `/projects/${projectId}/applications?limit=200`)
  application = (id: string) => this.request<Application>('GET', `/applications/${id}`)
  revision = (id: string) => this.request<Revision>('GET', `/revisions/${id}`)

  async validateFlow(model: unknown, module: string, flow: Flow): Promise<Issue[]> {
    const r = await this.request<{ issues: Issue[] }>('POST', '/models/validate-flow', { model, module, flow })
    return r.issues
  }

  /** Run a saved flow in the application's preview as a simulated end user. */
  runFlow(appId: string, flowKey: string, args: Record<string, unknown>, asUser: string, roles: string[]): Promise<RunResult> {
    return this.request<RunResult>('POST', `/applications/${appId}/preview/flows/${encodeURIComponent(flowKey)}/run`,
      { args }, { 'X-Preview-User': asUser, 'X-Preview-Roles': roles.join(',') })
  }

  createProposal(appId: string, model: unknown, title: string) {
    return this.request<ProposalSummary>('POST', `/applications/${appId}/proposals`, { model, title })
  }
}
