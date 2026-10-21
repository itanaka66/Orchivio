import { useState } from 'react'
import type { ApiClient } from '../api/client'
import { Danger, Feedback, Loading, Panel, useAction } from './kit'

type Json = Record<string, unknown>

interface ModuleRow {
  key: string
  entities: number
  flows: number
  processes: number
  pages: number
  enumerations: number
}

function extractModules(model: Json | null): ModuleRow[] {
  if (!model) return []
  return ((model.modules ?? []) as Array<{
    key: string
    entities?: unknown[]
    flows?: unknown[]
    processes?: unknown[]
    pages?: unknown[]
    enumerations?: unknown[]
  }>).map((m) => ({
    key: m.key,
    entities: m.entities?.length ?? 0,
    flows: m.flows?.length ?? 0,
    processes: m.processes?.length ?? 0,
    pages: m.pages?.length ?? 0,
    enumerations: m.enumerations?.length ?? 0,
  }))
}

/** Module management (FR-029). All mutations go through proposals. */
export function Modules({
  client,
  appId,
  model,
  canEdit,
}: {
  client: ApiClient
  appId: string
  model: Json | null
  canEdit: boolean
}) {
  const modules = extractModules(model)
  const [newKey, setNewKey] = useState('')
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renamedTo, setRenamedTo] = useState('')
  const { run, error, notice } = useAction(() => undefined)

  if (!model) return <Loading loading={false} error="No model loaded — open an application first." />

  const rawModules = (model.modules ?? []) as Json[]

  const addModule = () => {
    const key = newKey.trim()
    return client.createProposal(
      appId,
      { ...model, modules: [...rawModules, { key }] },
      `Add module "${key}"`,
    )
  }

  const renameModule = (from: string, to: string) =>
    client.createProposal(
      appId,
      { ...model, modules: rawModules.map((m) => (m['key'] === from ? { ...m, key: to } : m)) },
      `Rename module "${from}" → "${to}"`,
    )

  const deleteModule = (key: string) =>
    client.createProposal(
      appId,
      { ...model, modules: rawModules.filter((m) => m['key'] !== key) },
      `Delete module "${key}"`,
    )

  return (
    <Panel title="Modules">
      <p className="hint">
        Modules namespace entities, flows, processes and pages. All changes are submitted as proposals.
      </p>
      <Feedback error={error} notice={notice} />
      {modules.length === 0 && <p className="hint">No modules defined yet.</p>}
      <table className="trace">
        <thead>
          <tr>
            <th>Key</th>
            <th>Entities</th>
            <th>Flows</th>
            <th>Processes</th>
            <th>Pages</th>
            <th>Enums</th>
            {canEdit && <th><span className="sr-only">Actions</span></th>}
          </tr>
        </thead>
        <tbody>
          {modules.map((m) =>
            renaming === m.key ? (
              <tr key={m.key}>
                <td colSpan={canEdit ? 7 : 6}>
                  <form
                    className="inline"
                    onSubmit={(e) => {
                      e.preventDefault()
                      const to = renamedTo.trim()
                      if (!to || to === m.key) { setRenaming(null); return }
                      void run(() => renameModule(m.key, to), 'Rename proposal submitted.').then(() => setRenaming(null))
                    }}
                  >
                    <input
                      value={renamedTo}
                      onChange={(e) => setRenamedTo(e.target.value)}
                      pattern="[a-z][a-z0-9_]*"
                      autoFocus
                    />
                    <button type="submit" disabled={!renamedTo.trim() || renamedTo.trim() === m.key}>
                      Save
                    </button>
                    <button type="button" onClick={() => setRenaming(null)}>
                      Cancel
                    </button>
                  </form>
                </td>
              </tr>
            ) : (
              <tr key={m.key}>
                <td>
                  <code>{m.key}</code>
                </td>
                <td>{m.entities}</td>
                <td>{m.flows}</td>
                <td>{m.processes}</td>
                <td>{m.pages}</td>
                <td>{m.enumerations}</td>
                {canEdit && (
                  <td>
                    <button
                      type="button"
                      className="link"
                      onClick={() => {
                        setRenaming(m.key)
                        setRenamedTo(m.key)
                      }}
                    >
                      Rename
                    </button>
                    <Danger
                      label="Delete"
                      onConfirm={() =>
                        void run(
                          () => deleteModule(m.key),
                          `Deletion of "${m.key}" proposed.`,
                        )
                      }
                    />
                  </td>
                )}
              </tr>
            ),
          )}
        </tbody>
      </table>
      {canEdit && (
        <form
          className="inline"
          onSubmit={(e) => {
            e.preventDefault()
            if (!newKey.trim()) return
            void run(addModule, 'Module addition proposed.').then(() => setNewKey(''))
          }}
        >
          <input
            value={newKey}
            placeholder="my_module"
            pattern="[a-z][a-z0-9_]*"
            title="Lowercase letters, digits and underscores"
            onChange={(e) => setNewKey(e.target.value)}
          />
          <button type="submit" disabled={!newKey.trim()}>
            Add module
          </button>
        </form>
      )}
    </Panel>
  )
}
