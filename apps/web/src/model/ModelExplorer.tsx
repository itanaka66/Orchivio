import { useState } from 'react'
import { listFlows } from '../flow/model'

type Json = Record<string, unknown>
type Tab = 'entities' | 'enums' | 'pages' | 'security' | 'flows'

interface Field { key: string; type: string; required?: boolean; label?: string }
interface Entity { key: string; fields?: Field[]; label?: string }
interface EnumDef { key: string; values?: string[]; label?: string }
interface PageDef { key: string; path?: string; type?: string; entity?: string }
interface AccessRule { role: string; entity?: string; permission: string }
interface Security { roles?: string[]; access_rules?: AccessRule[] }
interface Module { key: string; entities?: Entity[]; enums?: EnumDef[]; pages?: PageDef[] }

interface Props { model: Json }

export function ModelExplorer({ model }: Props) {
  const [tab, setTab] = useState<Tab>('entities')
  const modules = (model.modules ?? []) as Module[]
  const security = (model.security ?? {}) as Security
  const allFlows = listFlows(model)

  const allEntities = modules.flatMap((m) => (m.entities ?? []).map((e) => ({ ...e, module: m.key })))
  const allEnums = modules.flatMap((m) => (m.enums ?? []).map((e) => ({ ...e, module: m.key })))
  const allPages = modules.flatMap((m) => (m.pages ?? []).map((p) => ({ ...p, module: m.key })))

  return (
    <div>
      <div className="tabs">
        {(['entities', 'enums', 'pages', 'security', 'flows'] as Tab[]).map((t) => (
          <button key={t} type="button" className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {tab === 'entities' && (
        <div>
          {allEntities.length === 0 && <p className="hint">No entities defined.</p>}
          {allEntities.map((e) => (
            <section key={`${e.module}.${e.key}`} className="card">
              <div className="panel-head">
                <h2>{e.key}{e.label ? ` — ${e.label}` : ''}</h2>
                <span className="hint">{e.module}</span>
              </div>
              {(e.fields ?? []).length > 0 ? (
                <table className="trace">
                  <thead><tr><th>Field</th><th>Type</th><th>Required</th></tr></thead>
                  <tbody>
                    {(e.fields ?? []).map((f) => (
                      <tr key={f.key}>
                        <td><code>{f.key}</code>{f.label ? ` — ${f.label}` : ''}</td>
                        <td>{f.type}</td>
                        <td>{f.required ? 'yes' : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <p className="hint">No fields.</p>}
            </section>
          ))}
        </div>
      )}

      {tab === 'enums' && (
        <div>
          {allEnums.length === 0 && <p className="hint">No enumerations defined.</p>}
          {allEnums.map((e) => (
            <section key={`${e.module}.${e.key}`} className="card">
              <div className="panel-head">
                <h2>{e.key}{e.label ? ` — ${e.label}` : ''}</h2>
                <span className="hint">{e.module}</span>
              </div>
              <p>{(e.values ?? []).map((v) => <span key={v} className="chip">{v}</span>)}</p>
            </section>
          ))}
        </div>
      )}

      {tab === 'pages' && (
        <div>
          {allPages.length === 0 && <p className="hint">No pages defined.</p>}
          {allPages.length > 0 && (
            <table className="trace">
              <thead><tr><th>Key</th><th>Path</th><th>Type</th><th>Entity</th><th>Module</th></tr></thead>
              <tbody>
                {allPages.map((p) => (
                  <tr key={`${p.module}.${p.key}`}>
                    <td><code>{p.key}</code></td>
                    <td>{p.path ?? ''}</td>
                    <td>{p.type ?? ''}</td>
                    <td>{p.entity ?? ''}</td>
                    <td>{p.module}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'security' && (
        <div>
          <section className="card">
            <h2>Roles</h2>
            {(security.roles ?? []).length === 0 && <p className="hint">No roles defined.</p>}
            {(security.roles ?? []).length > 0 && (
              <p>{(security.roles ?? []).map((r) => <span key={r} className="chip">{r}</span>)}</p>
            )}
          </section>
          <section className="card">
            <div className="panel-head"><h2>Access rules</h2></div>
            {(security.access_rules ?? []).length === 0 && <p className="hint">No access rules defined.</p>}
            {(security.access_rules ?? []).length > 0 && (
              <table className="trace">
                <thead><tr><th>Role</th><th>Entity</th><th>Permission</th></tr></thead>
                <tbody>
                  {(security.access_rules ?? []).map((r, i) => (
                    <tr key={i}>
                      <td>{r.role}</td>
                      <td>{r.entity ?? '— all —'}</td>
                      <td>{r.permission}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      )}

      {tab === 'flows' && (
        <div>
          {allFlows.length === 0 && <p className="hint">No flows defined.</p>}
          {allFlows.length > 0 && (
            <table className="trace">
              <thead><tr><th>Key</th><th>Kind</th><th>Steps</th><th>Module</th></tr></thead>
              <tbody>
                {allFlows.map((f) => (
                  <tr key={`${f.module}.${f.flow.key}`}>
                    <td><code>{f.flow.key}</code></td>
                    <td>{f.flow.kind}</td>
                    <td>{f.flow.steps.length}</td>
                    <td>{f.module}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
