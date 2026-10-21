import { useMemo, useState, type ReactNode } from 'react'
import type { Application, ApiClient, Org, Project } from '../api/client'
import { AppUsers } from './AppUsers'
import { AuditLog } from './AuditLog'
import { Branches } from './Branches'
import { Delivery } from './Delivery'
import { GitSync } from './GitSync'
import { Governance } from './Governance'
import { Integrations } from './Integrations'
import { Jobs } from './Jobs'
import { Logs } from './Logs'
import { McpServers } from './McpServers'
import { Modules } from './Modules'
import { Oidc } from './Oidc'
import { Processes } from './Processes'
import { Registry } from './Registry'
import { Stories } from './Stories'
import { TestRuns } from './TestRuns'
import { Webhooks } from './Webhooks'

type Json = Record<string, unknown>
const RANK: Record<string, number> = { viewer: 0, editor: 1, admin: 2, owner: 3 }

interface Section { key: string; label: string; group: 'Application' | 'Delivery' | 'Integrations' | 'Organization'; min: 'editor' | 'admin'; scope: 'app' | 'org' }

export const SECTIONS: Section[] = [
  { key: 'branches', label: 'Branches', group: 'Application', min: 'editor', scope: 'app' },
  { key: 'modules', label: 'Modules', group: 'Application', min: 'editor', scope: 'app' },
  { key: 'stories', label: 'Stories & requirements', group: 'Application', min: 'editor', scope: 'app' },
  { key: 'delivery', label: 'Environments & deployment', group: 'Delivery', min: 'editor', scope: 'app' },
  { key: 'jobs', label: 'Jobs & schedules', group: 'Application', min: 'editor', scope: 'app' },
  { key: 'processes', label: 'Processes & tasks', group: 'Application', min: 'editor', scope: 'app' },
  { key: 'tests', label: 'Test runs', group: 'Application', min: 'editor', scope: 'app' },
  { key: 'logs', label: 'Logs', group: 'Application', min: 'editor', scope: 'app' },
  { key: 'users', label: 'Application users', group: 'Application', min: 'admin', scope: 'app' },
  { key: 'oidc', label: 'Single sign-on (OIDC)', group: 'Application', min: 'admin', scope: 'app' },
  { key: 'webhooks', label: 'Webhooks', group: 'Integrations', min: 'admin', scope: 'app' },
  { key: 'integrations', label: 'Destinations & API import', group: 'Integrations', min: 'admin', scope: 'app' },
  { key: 'registry', label: 'Module registry', group: 'Integrations', min: 'editor', scope: 'app' },
  { key: 'git', label: 'Git sync', group: 'Integrations', min: 'admin', scope: 'app' },
  { key: 'mcp', label: 'MCP servers', group: 'Organization', min: 'admin', scope: 'org' },
  { key: 'governance', label: 'Governance', group: 'Organization', min: 'admin', scope: 'org' },
  { key: 'audit', label: 'Audit log', group: 'Organization', min: 'admin', scope: 'org' },
]

/** Role names and process keys the model defines, for the pickers. */
export function modelFacts(model: Json | null): { roles: string[]; processes: string[] } {
  const roles = ((model?.security as { roles?: string[] } | undefined)?.roles ?? []).filter((r) => typeof r === 'string')
  const processes = ((model?.modules ?? []) as { key: string; processes?: { key: string }[] }[]).flatMap((m) => (m.processes ?? []).map((p) => `${m.key}.${p.key}`))
  return { roles, processes }
}

export function visibleSections(role: string, hasApp: boolean): Section[] {
  const rank = RANK[role] ?? 0
  return SECTIONS.filter((s) => rank >= RANK[s.min]! && (s.scope === 'org' || hasApp))
}

interface Props { client: ApiClient; org: Org; project: Project; application?: Application; model: Json | null }

export function AdminWorkspace({ client, org, project, application, model }: Props) {
  const sections = useMemo(() => visibleSections(org.role, !!application), [org.role, application])
  const [key, setKey] = useState(sections[0]?.key ?? '')
  const current = sections.find((s) => s.key === key) ?? sections[0]
  const { roles, processes } = useMemo(() => modelFacts(model), [model])
  const appId = application?.id
  const moduleKeys = useMemo(() => ((model?.modules ?? []) as { key: string }[]).map((m) => m.key), [model])

  let body: ReactNode = <p className="hint">Nothing here is available for your role.</p>
  if (current) {
    const a = appId ?? ''
    body = {
      branches: <Branches client={client} appId={a} />,
      modules: <Modules client={client} appId={a} model={model} canEdit={RANK[org.role]! >= RANK.editor!} />,
      stories: <Stories client={client} projectId={project.id} />,
      delivery: <Delivery client={client} appId={a} currentRevisionId={application?.current_revision_id ?? null} />,
      jobs: <Jobs client={client} appId={a} roles={roles} />,
      processes: <Processes client={client} appId={a} processes={processes} />,
      tests: <TestRuns client={client} appId={a} revisionId={application?.current_revision_id ?? null} />,
      logs: <Logs client={client} appId={a} />,
      users: <AppUsers client={client} appId={a} roles={roles} />,
      oidc: <Oidc client={client} appId={a} roles={roles} />,
      webhooks: <Webhooks client={client} appId={a} />,
      integrations: <Integrations client={client} appId={a} />,
      registry: <Registry client={client} appId={a} orgId={org.id} modules={moduleKeys} canPublish={RANK[org.role]! >= RANK.admin!} />,
      git: <GitSync client={client} appId={a} />,
      mcp: <McpServers client={client} orgId={org.id} />,
      governance: <Governance client={client} orgId={org.id} />,
      audit: <AuditLog client={client} orgId={org.id} />,
    }[current.key] ?? body
  }

  const groups = ['Application', 'Delivery', 'Integrations', 'Organization'] as const
  return (
    <div className="admin">
      <nav aria-label="Administration" className="admin-nav">
        <p className="hint">{org.name} · {project.name}{application ? ` · ${application.name}` : ''}</p>
        {!application && <p className="hint">Choose an application to see its screens.</p>}
        {groups.map((g) => {
          const items = sections.filter((s) => s.group === g)
          return items.length === 0 ? null : (
            <div key={g}>
              <h3>{g}</h3>
              <ul className="list">{items.map((s) => (
                <li key={s.key}><button type="button" className={s.key === current?.key ? 'primary' : ''} aria-current={s.key === current?.key ? 'page' : undefined} onClick={() => setKey(s.key)}>{s.label}</button></li>
              ))}</ul>
            </div>
          )
        })}
      </nav>
      <section className="admin-body" key={`${current?.key}:${appId ?? org.id}`}>{body}</section>
    </div>
  )
}
