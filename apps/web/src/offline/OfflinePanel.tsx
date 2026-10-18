import { useState } from 'react'
import { useOffline } from './OfflineProvider'

const KIND: Record<string, string> = { requirement: 'Requirement', story: 'Story', feedback: 'Feedback', proposal: 'Flow change' }

/** Set up, unlock and review offline work. Renders nothing where offline storage is not available. */
export function OfflinePanel() {
  const off = useOffline()
  const [pass, setPass] = useState('')
  const [again, setAgain] = useState('')
  const [formError, setFormError] = useState('')
  const [confirmOff, setConfirmOff] = useState(false)
  if (!off || !off.supported) return null

  const summary = off.pending > 0 ? `${off.pending} unsent` : 'nothing waiting'
  return (
    <details className="offline-panel">
      <summary>Offline work{off.enabled ? ` · ${off.unlocked ? summary : 'locked'}` : ''}</summary>
      {!off.enabled && (
        <form onSubmit={async (e) => {
          e.preventDefault()
          if (pass !== again) return setFormError('The two passphrases differ.')
          setFormError('')
          try { await off.enable(pass); setPass(''); setAgain('') } catch (err) { setFormError(err instanceof Error ? err.message : 'Failed.') }
        }}>
          <p className="hint">Keep working without a connection. Changes you make offline (new requirements, stories, feedback and flow changes) wait on this device and are sent when you are back online. Saved copies and waiting changes are <strong>encrypted with a passphrase that only you know</strong>; if you lose it, they are lost. Editing or deleting existing items needs a connection.</p>
          <label className="field"><span>New passphrase (at least 10 characters)</span><input type="password" autoComplete="new-password" value={pass} onChange={(e) => setPass(e.target.value)} /></label>
          <label className="field"><span>Repeat the passphrase</span><input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} /></label>
          {(formError || off.error) && <p role="alert" className="sev-error">{formError || off.error}</p>}
          <button type="submit" className="primary" disabled={!pass}>Turn on offline work</button>
        </form>
      )}
      {off.enabled && off.foreign && (
        <div>
          <p role="alert" className="sev-warning">This device holds offline data of another account. It cannot be opened or sent while you are signed in as someone else.</p>
          <button type="button" className="danger" onClick={() => { if (confirmOff) { void off.disable(); setConfirmOff(false) } else setConfirmOff(true) }}>{confirmOff ? 'Really delete that account’s offline data?' : 'Delete that offline data'}</button>
        </div>
      )}
      {off.enabled && !off.foreign && !off.unlocked && (
        <form onSubmit={async (e) => { e.preventDefault(); if (await off.unlock(pass)) setPass('') }}>
          <label className="field"><span>Passphrase</span><input type="password" autoComplete="current-password" value={pass} onChange={(e) => setPass(e.target.value)} /></label>
          {off.error && <p role="alert" className="sev-error">{off.error}</p>}
          <p className="hint">{off.pending} change{off.pending === 1 ? '' : 's'} waiting on this device.</p>
          <div className="toolbar">
            <button type="submit" className="primary" disabled={!pass}>Unlock</button>
            <button type="button" className="danger" onClick={() => { if (confirmOff) { void off.disable(); setConfirmOff(false) } else setConfirmOff(true) }}>{confirmOff ? 'Really delete everything saved offline?' : 'Delete offline data'}</button>
          </div>
        </form>
      )}
      {off.enabled && off.unlocked && (
        <>
          <p className="hint">{off.online ? 'Online.' : 'Offline.'} {off.syncing ? 'Sending…' : off.lastSync ? `Last send: ${off.lastSync.sent} sent, ${off.lastSync.rejected} refused, ${off.lastSync.left} left.` : ''}</p>
          {off.lastSync?.halted === 'signed_out' && <p role="alert" className="sev-warning">Sign in again to send the waiting changes.</p>}
          {off.items.length === 0 && <p className="hint">Nothing is waiting to be sent.</p>}
          <ul className="list" aria-label="Changes waiting to be sent">
            {off.items.map((i) => (
              <li key={i.id} className="card slim">
                <div className="row"><strong>{KIND[i.kind] ?? i.kind}</strong> {i.label} <small className="hint">{i.status === 'rejected' ? 'refused' : i.status === 'sending' ? 'sending' : 'waiting'} · {new Date(i.createdAt).toLocaleString()}</small></div>
                {i.status === 'rejected' && (
                  <div className="sev-error">
                    <p>The server refused this change: {i.error}</p>
                    <div className="row">
                      <button type="button" onClick={() => void off.retry(i.id)}>Try again</button>
                      <button type="button" className="link" onClick={() => void off.discard(i.id)}>Discard</button>
                    </div>
                  </div>
                )}
                {i.status !== 'rejected' && <button type="button" className="link" onClick={() => void off.discard(i.id)} aria-label={`Discard ${i.label}`}>Discard</button>}
              </li>
            ))}
          </ul>
          <div className="toolbar">
            <button type="button" className="primary" disabled={!off.online || off.syncing || off.pending === 0} onClick={() => void off.syncNow()}>Send now</button>
            <button type="button" onClick={off.lock}>Lock</button>
            <button type="button" className="danger" onClick={() => { if (confirmOff) { void off.disable(); setConfirmOff(false) } else setConfirmOff(true) }}>{confirmOff ? `Delete ${off.pending} waiting change${off.pending === 1 ? '' : 's'} and all saved copies?` : 'Turn off and delete'}</button>
          </div>
        </>
      )}
    </details>
  )
}
