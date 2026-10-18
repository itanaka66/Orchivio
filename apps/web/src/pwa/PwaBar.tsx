import { usePwa } from './usePwa'

/** Offline notice, "update ready" and "install" prompts. Renders nothing when there is nothing to say. */
export function PwaBar() {
  const { online, update, applyUpdate, canInstall, install } = usePwa()
  if (online && !update && !canInstall) return null
  return (
    <div className="pwa-bar" role="region" aria-label="App status">
      {!online && <p role="status"><strong>You are offline.</strong> Studio cannot load or save anything until the connection is back; what is on screen has not been sent.</p>}
      {update && (
        <p role="status">A new version of Studio is ready. <button type="button" onClick={applyUpdate}>Reload to update</button> <small className="hint">Unsaved edits are lost on reload, so save first.</small></p>
      )}
      {canInstall && <p><button type="button" onClick={() => void install()}>Install Studio as an app</button></p>}
    </div>
  )
}
