// Thin client for the Orchivio API (docs/API-SPEC.md). Cookie session + CSRF header.
import type { Flow, Issue, RunResult } from '../flow/types'

/** `If-Match` for a change to an item at the version the person saw (the server answers 409 with its current copy if the item has moved on). */
const versionHeader = (v?: number): Record<string, string> => (v === undefined ? {} : { 'If-Match': `"${v}"` })

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

export type RequirementStatus = 'draft' | 'accepted' | 'implemented' | 'rejected'
export type StoryStatus = 'proposed' | 'backlog' | 'ready' | 'in_progress' | 'done' | 'rejected'
export interface Requirement { id: string; title: string; body: string; status: RequirementStatus; source: string; version?: number }
export interface Story { id: string; title: string; body: string; acceptance: string; status: StoryStatus; source: string; position: number; requirement_id: string | null; version?: number }
export interface TraceLink { id: string; from_type: 'requirement' | 'story'; from_id: string; to_type: 'story' | 'element' | 'revision' | 'test'; to_id: string }
export interface TraceTarget { id: string; name?: string | null; title?: string | null; exists: boolean }
export interface TraceRow { id: string; title: string; status: string; stories?: string[]; elements: TraceTarget[]; tests: TraceTarget[]; revisions: string[] }
export interface Traceability {
  requirements: TraceRow[]
  stories: TraceRow[]
  coverage: { requirements: number; with_stories: number; with_elements: number; with_tests: number; with_revisions: number; dangling_elements: number; dangling_tests: number }
}
export interface FeedbackItem { id: string; page_key: string; element_id: string | null; body: string; status: 'new' | 'clustered' | 'dismissed'; has_screenshot: boolean; story_id: string | null; created_at: string }
export interface FeedbackInput { page_key: string; body: string; element_id?: string | null; screenshot_base64?: string | null }

export interface Finding { rule: string; severity: string; message: string; path?: string; element_id?: string }
export interface Change { kind: string; path: string; summary: string; element_id?: string }
export interface ProposalDetail {
  id: string; title: string; status: string; approval_level: string
  diff: { level: string; security_changed: boolean; changes: Change[] }
  lint: Finding[]
  warnings: string[]
}
export interface AnalysisResult { summary: string; roles: string[]; entities: string[]; open_questions: string[]; assumptions: string[] }
export interface RevisionSummary { id: string; revision_no: number; created_at: string }
export interface LintResult { findings: Finding[] }
export interface TestRunSummary { total: number; passed: number; failed: number; skipped: number }
export interface TestCaseResult { id: string; title: string; outcome: string }
export interface TestRunResult { id: string; status: string; revision_id: string; summary: TestRunSummary; cases: TestCaseResult[] }

export interface PreviewActor { user: string; roles: string[] }

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

  /** Absolute-path URL of an API route, for links the browser follows itself (downloads, screenshots). */
  url(path: string): string { return `${this.base}${path}` }

  async request<T>(method: string, path: string, body?: unknown, extraHeaders: Record<string, string> = {}): Promise<T> {
    const headers: Record<string, string> = { ...extraHeaders }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    return this.send<T>(method, path, body === undefined ? undefined : JSON.stringify(body), headers)
  }

  /** Send text as the raw request body (for endpoints that take a document, such as an OpenAPI description). */
  requestText<T>(method: string, path: string, text: string, contentType = 'text/plain'): Promise<T> {
    return this.send<T>(method, path, text, { 'Content-Type': contentType })
  }

  private async send<T>(method: string, path: string, payload: string | undefined, extra: Record<string, string>): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json', ...extra }
    // Cookie-authenticated unsafe requests must carry the CSRF token returned at login.
    if (method !== 'GET' && this.csrf) headers['X-CSRF-Token'] = this.csrf
    const res = await this.doFetch(`${this.base}${path}`, { method, headers, credentials: 'same-origin', body: payload })
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
  createOrganization = (name: string, slug: string) =>
    this.request<Org>('POST', '/organizations', { name, slug })
  projects = (orgId: string) => this.request<Page<Project>>('GET', `/organizations/${orgId}/projects?limit=200`)
  createProject = (orgId: string, name: string, slug: string) =>
    this.request<Project>('POST', `/organizations/${orgId}/projects`, { name, slug })
  applications = (projectId: string) => this.request<Page<Application>>('GET', `/projects/${projectId}/applications?limit=200`)
  createApplication = (projectId: string, key: string, name: string) =>
    this.request<Application>('POST', `/projects/${projectId}/applications`, { key, name })
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

  // --- requirements, stories, traceability (docs/AGILE.md)
  requirements = (projectId: string) => this.request<{ items: Requirement[] }>('GET', `/projects/${projectId}/requirements`)
  createRequirement = (projectId: string, title: string, body = '') => this.request<Requirement>('POST', `/projects/${projectId}/requirements`, { title, body })
  updateRequirement = (projectId: string, id: string, patch: Partial<Pick<Requirement, 'title' | 'body' | 'status'>>, ifMatch?: number) => this.request<Requirement>('PATCH', `/projects/${projectId}/requirements/${id}`, patch, versionHeader(ifMatch))
  deleteRequirement = (projectId: string, id: string, ifMatch?: number) => this.request<void>('DELETE', `/projects/${projectId}/requirements/${id}`, undefined, versionHeader(ifMatch))
  extractRequirements = (appId: string, text: string) =>
    this.request<{ requirements: Requirement[]; skipped_duplicates: number }>('POST', `/applications/${appId}/requirements/extract`, { text })
  stories = (projectId: string) => this.request<{ items: Story[] }>('GET', `/projects/${projectId}/stories`)
  createStory = (projectId: string, title: string, requirementId?: string) =>
    this.request<Story>('POST', `/projects/${projectId}/stories`, { title, ...(requirementId ? { requirement_id: requirementId } : {}) })
  updateStory = (projectId: string, id: string, patch: Partial<Pick<Story, 'title' | 'body' | 'acceptance' | 'status' | 'position' | 'requirement_id'>>, ifMatch?: number) =>
    this.request<Story>('PATCH', `/projects/${projectId}/stories/${id}`, patch, versionHeader(ifMatch))
  deleteStory = (projectId: string, id: string, ifMatch?: number) => this.request<void>('DELETE', `/projects/${projectId}/stories/${id}`, undefined, versionHeader(ifMatch))
  traceLinks = (projectId: string) => this.request<{ items: TraceLink[] }>('GET', `/projects/${projectId}/trace-links`)
  createTraceLink = (projectId: string, link: Omit<TraceLink, 'id'>) => this.request<TraceLink>('POST', `/projects/${projectId}/trace-links`, link)
  deleteTraceLink = (projectId: string, id: string) => this.request<void>('DELETE', `/projects/${projectId}/trace-links/${id}`)
  traceability = (projectId: string) => this.request<Traceability>('GET', `/projects/${projectId}/traceability`)
  testCases = (projectId: string, q = '') => this.request<{ items: { id: string; title: string }[]; total: number }>('GET', `/projects/${projectId}/test-cases?q=${encodeURIComponent(q)}&limit=100`)

  // --- preview data, as a simulated end user
  private asActor = (a: PreviewActor) => ({ 'X-Preview-User': a.user, 'X-Preview-Roles': a.roles.join(',') })
  previewList = (appId: string, entity: string, actor: PreviewActor) =>
    this.request<{ items: Record<string, unknown>[]; total: number }>('GET', `/applications/${appId}/preview/data/${entity}?limit=50`, undefined, this.asActor(actor))
  previewCreate = (appId: string, entity: string, values: Record<string, unknown>, actor: PreviewActor) =>
    this.request<Record<string, unknown>>('POST', `/applications/${appId}/preview/data/${entity}`, values, this.asActor(actor))
  previewUpdate = (appId: string, entity: string, id: string, values: Record<string, unknown>, actor: PreviewActor) =>
    this.request<Record<string, unknown>>('PATCH', `/applications/${appId}/preview/data/${entity}/${encodeURIComponent(id)}`, values, this.asActor(actor))
  previewReset = (appId: string) => this.request<unknown>('POST', `/applications/${appId}/preview/reset`, { seed: { rows_per_entity: 8, seed: 1, users: ['preview-user'] } })

  // --- preview feedback
  feedback = (appId: string, status?: FeedbackItem['status']) => this.request<{ items: FeedbackItem[] }>('GET', `/applications/${appId}/feedback${status ? `?status=${status}` : ''}`)
  submitFeedback = (appId: string, input: FeedbackInput) => this.request<FeedbackItem>('POST', `/applications/${appId}/feedback`, input)
  dismissFeedback = (appId: string, id: string) => this.request<FeedbackItem>('PATCH', `/applications/${appId}/feedback/${id}`, { status: 'dismissed' })
  clusterFeedback = (appId: string) => this.request<{ stories: Story[]; unclustered: number }>('POST', `/applications/${appId}/feedback/cluster`)
  screenshotUrl = (appId: string, id: string) => `${this.base}/applications/${appId}/feedback/${id}/screenshot`

  createProposal(appId: string, model: unknown, title: string) {
    return this.request<ProposalSummary>('POST', `/applications/${appId}/proposals`, { model, title })
  }

  revisions = (appId: string, limit = 20, offset = 0) =>
    this.request<Page<RevisionSummary>>('GET', `/applications/${appId}/revisions?limit=${limit}&offset=${offset}`)

  lint = (revisionId: string) =>
    this.request<LintResult>('GET', `/revisions/${revisionId}/lint`)

  proposals = (appId: string, limit = 20, offset = 0) =>
    this.request<Page<ProposalSummary>>('GET', `/applications/${appId}/proposals?limit=${limit}&offset=${offset}`)

  proposalDetail = (appId: string, proposalId: string) =>
    this.request<ProposalDetail>('GET', `/applications/${appId}/proposals/${proposalId}`)

  approveProposal = (appId: string, proposalId: string, acknowledgeRisks = false) =>
    this.request<ProposalSummary>('POST', `/applications/${appId}/proposals/${proposalId}/approve`, { acknowledge_risks: acknowledgeRisks })

  rejectProposal = (appId: string, proposalId: string) =>
    this.request<ProposalSummary>('POST', `/applications/${appId}/proposals/${proposalId}/reject`, {})

  analyzeRequirements = (appId: string, text: string) =>
    this.request<AnalysisResult>('POST', `/applications/${appId}/ai/analyze`, { text })

  proposeModel = (appId: string, text: string) =>
    this.request<ProposalSummary>('POST', `/applications/${appId}/ai/propose`, { text })

  explainElement = (appId: string, elementId: string) =>
    this.request<{ explanation: string }>('GET', `/applications/${appId}/ai/explain/${encodeURIComponent(elementId)}`)

  planRequirements = (appId: string, text: string) =>
    this.request<{ plan: string }>('POST', `/applications/${appId}/ai/plan`, { text })

  createTestRun = (appId: string, revisionId: string) =>
    this.request<TestRunResult>('POST', `/applications/${appId}/test-runs`, { revision_id: revisionId })

  testRuns = (appId: string, limit = 20, offset = 0) =>
    this.request<Page<TestRunResult>>('GET', `/applications/${appId}/test-runs?limit=${limit}&offset=${offset}`)

  testRun = (appId: string, id: string) =>
    this.request<TestRunResult>('GET', `/applications/${appId}/test-runs/${id}`)

  repairTests = (appId: string, id: string) =>
    this.request<TestRunResult>('POST', `/applications/${appId}/test-runs/${id}/repair`, {})
}
