// Undo/redo over immutable snapshots. Because Flow values are immutable, a snapshot is just a
// reference, so keeping many is cheap.

export interface History<T> {
  past: T[]
  present: T
  future: T[]
}

const LIMIT = 100

export const initial = <T,>(present: T): History<T> => ({ past: [], present, future: [] })

/** Record a change as one undo step. Identical values (by reference) are ignored. */
export function push<T>(h: History<T>, next: T): History<T> {
  if (next === h.present) return h
  return { past: [...h.past, h.present].slice(-LIMIT), present: next, future: [] }
}

/** Change the present value without creating an undo step (for example while dragging). */
export function replace<T>(h: History<T>, next: T): History<T> {
  return next === h.present ? h : { ...h, present: next }
}

/** Commit an earlier `replace`d series of changes as a single undo step. */
export function commit<T>(h: History<T>, base: T): History<T> {
  if (base === h.present) return h
  return { past: [...h.past, base].slice(-LIMIT), present: h.present, future: [] }
}

export function undo<T>(h: History<T>): History<T> {
  const prev = h.past[h.past.length - 1]
  if (prev === undefined) return h
  return { past: h.past.slice(0, -1), present: prev, future: [h.present, ...h.future] }
}

export function redo<T>(h: History<T>): History<T> {
  const next = h.future[0]
  if (next === undefined) return h
  return { past: [...h.past, h.present], present: next, future: h.future.slice(1) }
}

export const canUndo = <T,>(h: History<T>) => h.past.length > 0
export const canRedo = <T,>(h: History<T>) => h.future.length > 0
