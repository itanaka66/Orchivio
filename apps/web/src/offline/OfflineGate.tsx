import { useEffect, useState } from 'react'
import type { Application, Project } from '../api/client'
import { listFlows } from '../flow/model'
import type { Flow } from '../flow/types'
import { OfflinePanel } from './OfflinePanel'
import { useOffline } from './OfflineProvider'
import { INDEX_KEY, modelKey, PROFILE_KEY, type IndexEntry, type Profile, type SavedModel } from './index'

export interface GateBoard { project: Project; application?: Application; canEdit: boolean }
export interface GateFlow { application: Application; model: Record<string, unknown>; module: string; flow: Flow }

/**
 * What the Studio shows when it starts without a connection and the sign-in cannot be checked: the offline data on this device, once unlocked. Reading saved
 * boards and models and adding new items works; everything else waits for a connection. Nothing here authenticates anyone to the server: queued changes are sent
 * only after signing in again.
 */
export function OfflineGate({ onBoard, onOpenFlow, children }: { onBoard: (b: GateBoard) => void; onOpenFlow: (o: GateFlow) => void; children: React.ReactNode }) {
  const off = useOffline()
  const [entries, setEntries] = useState<IndexEntry[] | null>(null)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [chosen, setChosen] = useState<{ application: Application; saved: SavedModel } | null>(null)

  useEffect(() => {
    if (!off?.unlocked) { setEntries(null); return }
    let live = true
    void Promise.all([off.loadSaved<IndexEntry[]>(INDEX_KEY), off.loadSaved<Profile>(PROFILE_KEY)]).then(([i, p]) => { if (live) { setEntries(i?.data ?? []); setProfile(p?.data ?? null) } })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [off?.unlocked])

  const openFlows = async (a: { id: string; name: string }) => {
    const saved = await off?.loadSaved<SavedModel>(modelKey(a.id))
    if (saved) setChosen({ application: { id: a.id, project_id: '', key: '', name: a.name, current_revision_id: null, current_revision_no: null }, saved: saved.data })
  }

  return (
    <main className="page">
      <h1>Orchivio Studio · Offline</h1>
      <p role="status" className="sev-warning">The Studio cannot reach the server. You can work with the data saved on this device; changes are sent when you are online and signed in.</p>
      <OfflinePanel />
      {!off?.supported && <p className="hint">Offline storage is not available in this browser.</p>}
      {off?.supported && !off.enabled && <p className="hint">Offline work was never set up on this device, so there is nothing to open. Connect to the server to sign in.</p>}
      {off?.unlocked && entries && (
        <section className="card" aria-label="Saved projects">
          <h2>{profile ? `Saved for ${profile.displayName}` : 'Saved projects'}</h2>
          {entries.length === 0 && <p className="hint">Nothing has been saved yet. Open a project's story board while online with offline work unlocked.</p>}
          <ul className="list">
            {entries.map((e) => (
              <li key={e.projectId} className="card slim">
                <div className="row"><strong>{e.projectName}</strong> <small className="hint">{e.organization}</small>
                  <button type="button" onClick={() => onBoard({ project: { id: e.projectId, organization_id: '', name: e.projectName, slug: '' }, canEdit: e.canEdit, application: e.applications[0] ? { id: e.applications[0].id, project_id: e.projectId, key: '', name: e.applications[0].name, current_revision_id: null, current_revision_no: null } : undefined })}>Open story board</button></div>
                {e.applications.filter((a) => a.hasModel).map((a) => <div key={a.id} className="row"><span>{a.name}</span><button type="button" onClick={() => void openFlows(a)}>Open flows</button></div>)}
              </li>
            ))}
          </ul>
        </section>
      )}
      {chosen && (
        <section className="card" aria-label="Saved flows">
          <h2>Flows of {chosen.application.name}</h2>
          <p className="hint">From the copy saved on this device. Saving creates a proposal that is sent when you are online; live validation and test runs need a connection.</p>
          <ul className="list">
            {listFlows(chosen.saved.model).map((f) => (
              <li key={`${f.module}.${f.flow.key}`}><button type="button" onClick={() => onOpenFlow({ application: chosen.application, model: chosen.saved.model, module: f.module, flow: f.flow })}>{f.flow.key} <small>({f.flow.kind}, {f.flow.steps.length} steps)</small></button></li>
            ))}
          </ul>
          <button type="button" onClick={() => setChosen(null)}>Close</button>
        </section>
      )}
      {children}
    </main>
  )
}
