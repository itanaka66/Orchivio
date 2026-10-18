import type { Offline } from './OfflineProvider'

// What is remembered for offline reading beyond a project's board: a small index of projects and applications, and each application's model.

export interface IndexEntry {
  projectId: string
  projectName: string
  organization: string
  canEdit: boolean
  applications: { id: string; name: string; hasModel: boolean }[]
}
export interface Profile { displayName: string }

export const INDEX_KEY = 'index'
export const PROFILE_KEY = 'profile'
export const modelKey = (applicationId: string) => `model:${applicationId}`

/** Add or update a project in the saved index (does nothing while the vault is locked). */
export async function remember(off: Offline | null, entry: IndexEntry): Promise<void> {
  if (!off || !off.unlocked) return
  const saved = await off.loadSaved<IndexEntry[]>(INDEX_KEY)
  const rest = (saved?.data ?? []).filter((e) => e.projectId !== entry.projectId)
  await off.snapshot(INDEX_KEY, [...rest, entry].sort((a, b) => a.projectName.localeCompare(b.projectName)))
}

export interface SavedModel { name: string; model: Record<string, unknown> }
export const saveModel = (off: Offline | null, applicationId: string, saved: SavedModel) => off?.snapshot(modelKey(applicationId), saved)
