import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, type ApiClient } from '../api/client'

/** Largest screenshot the server accepts (docs/AGILE.md). */
export const MAX_SCREENSHOT_BYTES = 512 * 1024
export const ELEMENT_ATTR = 'data-orchivio-element'
export const PAGE_ATTR = 'data-orchivio-page'

/** The model element a click landed on, and the page it belongs to, read from `data-orchivio-*` attributes on the rendered UI. */
export function targetOf(node: EventTarget | null): { element: string | null; page: string | null } {
  const el = node instanceof Element ? node : null
  return {
    element: el?.closest(`[${ELEMENT_ATTR}]`)?.getAttribute(ELEMENT_ATTR) ?? null,
    page: el?.closest(`[${PAGE_ATTR}]`)?.getAttribute(PAGE_ATTR) ?? null,
  }
}

export function toBase64(bytes: Uint8Array): string {
  let s = ''
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(s)
}

const isImage = (b: Uint8Array) =>
  (b.length > 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) || (b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff)

/** One frame of the screen the person chooses to share, as PNG bytes (browser permission prompt; nothing is captured silently). */
async function captureScreen(): Promise<Uint8Array> {
  const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false })
  try {
    const video = document.createElement('video')
    video.srcObject = stream
    video.muted = true
    await video.play()
    const canvas = document.createElement('canvas')
    // keep the image small enough for the server's limit
    const scale = Math.min(1, 1280 / (video.videoWidth || 1280))
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale))
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale))
    canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
    if (!blob) throw new Error('Could not capture the screen.')
    return new Uint8Array(await blob.arrayBuffer())
  } finally {
    stream.getTracks().forEach((t) => t.stop())
  }
}

interface Props {
  client: Pick<ApiClient, 'submitFeedback'>
  applicationId: string
  /** Page key used when the clicked element is not inside an element marked with `data-orchivio-page`. */
  page: string
}

export function FeedbackWidget({ client, applicationId, page }: Props) {
  const [open, setOpen] = useState(false)
  const [picking, setPicking] = useState(false)
  const [body, setBody] = useState('')
  const [element, setElement] = useState<string | null>(null)
  const [pageKey, setPageKey] = useState(page)
  const [shot, setShot] = useState<Uint8Array | null>(null)
  const [error, setError] = useState('')
  const [sent, setSent] = useState(false)
  const [busy, setBusy] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => { if (!element) setPageKey(page) }, [page, element])

  // While picking, the next click anywhere outside the widget chooses the element and is not passed on to the page.
  useEffect(() => {
    if (!picking) return
    const onClick = (e: MouseEvent) => {
      if (root.current?.contains(e.target as Node)) return
      e.preventDefault()
      e.stopPropagation()
      const t = targetOf(e.target)
      setElement(t.element)
      setPageKey(t.page ?? page)
      setPicking(false)
      setOpen(true)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { setPicking(false); setOpen(true) } }
    document.addEventListener('click', onClick, true)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('click', onClick, true); document.removeEventListener('keydown', onKey) }
  }, [picking, page])

  const attach = useCallback(async (file: Blob | null | undefined) => {
    if (!file) return
    const bytes = new Uint8Array(await file.arrayBuffer())
    if (!isImage(bytes)) return setError('Only PNG or JPEG images can be attached.')
    if (bytes.length > MAX_SCREENSHOT_BYTES) return setError('That image is larger than 512 KiB.')
    setError('')
    setShot(bytes)
  }, [])

  const reset = () => { setBody(''); setElement(null); setShot(null); setError(''); setPageKey(page) }

  const submit = async () => {
    setBusy(true)
    setError('')
    try {
      await client.submitFeedback(applicationId, { page_key: pageKey, body: body.trim(), element_id: element, screenshot_base64: shot ? toBase64(shot) : null })
      setSent(true)
      reset()
      setOpen(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not send the feedback.')
    } finally { setBusy(false) }
  }

  const canCapture = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getDisplayMedia

  return (
    <div ref={root} className="feedback-widget">
      {!open && !picking && (
        <button type="button" className="primary" onClick={() => { setSent(false); setOpen(true) }}>Feedback</button>
      )}
      {sent && !open && <p role="status" className="hint">Thanks, your feedback was sent.</p>}
      {picking && <p role="status" className="feedback-banner">Click the part of the page you mean, or press Escape.</p>}
      {open && (
        <form role="dialog" aria-label="Send feedback" className="card feedback-dialog" onSubmit={(e) => { e.preventDefault(); void submit() }}
          onPaste={(e) => { const f = [...e.clipboardData.files].find((x) => x.type.startsWith('image/')); if (f) { e.preventDefault(); void attach(f) } }}>
          <h2>Send feedback</h2>
          <label className="field"><span>What should change?</span>
            <textarea required rows={4} maxLength={2000} value={body} onChange={(e) => setBody(e.target.value)} /></label>
          <p className="hint">Page: <code>{pageKey}</code>{element ? <> · element <code>{element.slice(0, 8)}</code> <button type="button" className="link" onClick={() => setElement(null)}>clear</button></> : null}</p>
          <div className="toolbar">
            <button type="button" onClick={() => { setOpen(false); setPicking(true) }}>Pick an element</button>
            <label className="file"><span className="sr-only">Attach an image</span>
              <input type="file" accept="image/png,image/jpeg" onChange={(e) => void attach(e.target.files?.[0])} /></label>
            {canCapture && <button type="button" onClick={async () => { try { await attach(new Blob([(await captureScreen()) as BlobPart])) } catch { setError('Screen capture was cancelled or is not allowed.') } }}>Capture screen</button>}
          </div>
          {shot && <p className="hint">Image attached ({Math.ceil(shot.length / 1024)} KiB). <button type="button" className="link" onClick={() => setShot(null)}>Remove</button></p>}
          {error && <p role="alert" className="sev-error">{error}</p>}
          <div className="toolbar">
            <button type="submit" className="primary" disabled={busy || !body.trim()}>{busy ? 'Sending…' : 'Send'}</button>
            <button type="button" onClick={() => { setOpen(false); reset() }}>Cancel</button>
          </div>
        </form>
      )}
    </div>
  )
}
