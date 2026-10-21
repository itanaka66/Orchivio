import { useState } from 'react'
import { errMsg, Pill } from '../admin/kit'
import type { AnalysisResult, ApiClient, Application, ProposalSummary } from '../api/client'

type Step = 'input' | 'analyzing' | 'analysis' | 'proposing' | 'done' | 'error'

interface Props { client: ApiClient; application: Application; canEdit: boolean; onReview: () => void }

export function AIBuilder({ client, application, canEdit, onReview }: Props) {
  const [step, setStep] = useState<Step>('input')
  const [text, setText] = useState('')
  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null)
  const [proposal, setProposal] = useState<ProposalSummary | null>(null)
  const [error, setError] = useState('')

  const analyze = async () => {
    setStep('analyzing')
    setError('')
    try {
      const r = await client.analyzeRequirements(application.id, text)
      setAnalysis(r)
      setStep('analysis')
    } catch (e) {
      setError(errMsg(e))
      setStep('error')
    }
  }

  const propose = async () => {
    setStep('proposing')
    setError('')
    try {
      const r = await client.proposeModel(application.id, text)
      setProposal(r)
      setStep('done')
    } catch (e) {
      setError(errMsg(e))
      setStep('error')
    }
  }

  const reset = () => { setStep('input'); setText(''); setAnalysis(null); setProposal(null); setError('') }

  if (step === 'input') return (
    <section className="card" aria-label="AI Builder">
      <div className="panel-head"><h2>Build with AI</h2></div>
      <p className="hint">Describe what you want to build in plain language. The AI will generate a model proposal.</p>
      <label className="field">
        <span>Requirements</span>
        <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)}
          placeholder="I need an inventory management system with products, suppliers and purchase orders…" />
      </label>
      {canEdit && (
        <div className="toolbar">
          <button type="button" className="primary" disabled={!text.trim()} onClick={() => void analyze()}>
            Analyze first
          </button>
          <button type="button" disabled={!text.trim()} onClick={() => void propose()}>
            Propose directly
          </button>
        </div>
      )}
    </section>
  )

  if (step === 'analyzing' || step === 'proposing') return (
    <section className="card">
      <p>{step === 'analyzing' ? 'Analyzing requirements…' : 'Generating model proposal…'}</p>
    </section>
  )

  if (step === 'error') return (
    <section className="card">
      <p role="alert" className="sev-error">{error}</p>
      <button type="button" onClick={reset}>Try again</button>
    </section>
  )

  if (step === 'analysis' && analysis) return (
    <section className="card" aria-label="Analysis result">
      <div className="panel-head"><h2>Analysis</h2></div>
      <p>{analysis.summary}</p>
      {analysis.roles.length > 0 && (
        <><h3>Roles</h3><p>{analysis.roles.map((r) => <span key={r} className="chip">{r}</span>)}</p></>
      )}
      {analysis.entities.length > 0 && (
        <><h3>Entities</h3><p>{analysis.entities.map((e) => <span key={e} className="chip">{e}</span>)}</p></>
      )}
      {analysis.open_questions.length > 0 && (
        <><h3>Open questions</h3><ul className="list">{analysis.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul></>
      )}
      {canEdit && (
        <div className="toolbar">
          <button type="button" className="primary" onClick={() => void propose()}>Build proposal</button>
          <button type="button" onClick={reset}>Start over</button>
        </div>
      )}
    </section>
  )

  if (step === 'done' && proposal) return (
    <section className="card" aria-label="Proposal created">
      <div className="panel-head"><h2>Proposal created</h2></div>
      <p>
        <Pill value={proposal.status} />{' '}
        {proposal.approval_level} · {proposal.diff.changes.length} change{proposal.diff.changes.length === 1 ? '' : 's'}
      </p>
      <p>The proposal is waiting for approval. Go to <strong>Review Proposals</strong> to approve or reject it.</p>
      <div className="toolbar">
        <button type="button" className="primary" onClick={onReview}>Review proposals</button>
        <button type="button" onClick={reset}>Build another</button>
      </div>
    </section>
  )

  return null
}
