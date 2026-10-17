import { STEP_META, addableTypes } from '../flow/catalog'
import type { Flow, StepType } from '../flow/types'

export const DRAG_TYPE = 'application/x-orchivio-step'

interface Props {
  kind: Flow['kind']
  onAdd: (type: StepType) => void
  disabled?: boolean
}

/** Buttons add a step at the centre of the canvas (keyboard friendly); they can also be dragged. */
export function Palette({ kind, onAdd, disabled }: Props) {
  return (
    <nav className="palette" aria-label="Add a step">
      <h2>Steps</h2>
      <ul>
        {addableTypes(kind).map((t) => (
          <li key={t}>
            <button
              type="button"
              disabled={disabled}
              draggable={!disabled}
              onDragStart={(e) => {
                e.dataTransfer.setData(DRAG_TYPE, t)
                e.dataTransfer.effectAllowed = 'move'
              }}
              onClick={() => onAdd(t)}
              title={STEP_META[t].description}
              aria-label={`Add ${STEP_META[t].label} step: ${STEP_META[t].description}`}
            >
              <span aria-hidden="true" className="step-glyph">{STEP_META[t].glyph}</span> {STEP_META[t].label}
            </button>
          </li>
        ))}
      </ul>
      <p className="hint">Click to add, or drag onto the canvas. Select a step to edit it and choose what comes next.</p>
    </nav>
  )
}
