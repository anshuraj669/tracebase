import { useEffect, useState } from 'react'
import { api } from '../api'
import Markdown from '../components/Markdown'

const MODES = [
  { id: 'ask', label: 'Ask', hint: 'Ask in plain English — answered from your code, with file:line citations' },
  { id: 'search', label: 'Semantic', hint: 'Meaning-based + keyword search, e.g. “function that resizes uploaded images”' },
  { id: 'exact', label: 'Exact', hint: 'Literal text or symbol, e.g. sendMail(' },
  { id: 'regex', label: 'Regex', hint: 'Regular expression, e.g. Route::(get|post)' },
]
const EXAMPLES = {
  ask: ['Where did I implement JWT refresh tokens before?', 'Show every project that integrates Stripe webhooks', 'How do I send password reset emails?'],
  search: ['function that resizes uploaded images', 'send password reset email', 'apply coupon discount to cart'],
  exact: ['sendMail(', 'env(', 'JWTAuth'],
  regex: ['Route::(get|post)', 'sk_(live|test)_', 'function\\s+\\w+Token'],
}

export default function SearchPage({ onOpen, llm, pending }) {
  const [mode, setMode] = useState('ask')
  const [q, setQ] = useState('')
  const [repo, setRepo] = useState('')
  const [repos, setRepos] = useState([])
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    api.repos().then(setRepos).catch(() => {})
  }, [])

  const run = async (query = q, m = mode) => {
    if (!query.trim()) return
    setQ(query)
    setBusy(true)
    setErr(null)
    setRes(null)
    const t0 = performance.now()
    try {
      let data
      if (m === 'ask') data = { kind: 'ask', ...(await api.ask(query, repo)) }
      else if (m === 'search') data = { kind: 'search', ...(await api.search(query, repo)) }
      else data = { kind: 'grep', ...(await api.grep(query, repo, m === 'regex')) }
      data.ms = Math.round(performance.now() - t0)
      setRes(data)
    } catch (e) {
      setErr(e.message)
    }
    setBusy(false)
  }

  useEffect(() => {
    if (pending?.q) { setMode('ask'); run(pending.q, 'ask') }
  }, [pending?.at])

  const current = MODES.find((x) => x.id === mode)

  return (
    <div className="search">
      <div className="card search-box">
      <div className="hero">
        <h2>Search everything you've ever written.</h2>
        <p className="muted">Every repo, clone and old .zip on your server: indexed by function, searchable by meaning, explained by a local LLM.</p>
      </div>
      <div className="modes" role="tablist">
        {MODES.map((m) => (
          <button key={m.id} role="tab" aria-selected={mode === m.id} className={mode === m.id ? 'on' : ''}
            onClick={() => { setMode(m.id); setRes(null) }}>
            {m.label}
          </button>
        ))}
      </div>
      <form className="qbar" onSubmit={(e) => { e.preventDefault(); run() }}>
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder={current.hint} />
        <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Repository filter">
          <option value="">All repos</option>
          {repos.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
        </select>
        <button className="primary" disabled={busy}>{busy ? (mode === 'ask' ? 'Thinking…' : 'Searching…') : mode === 'ask' ? 'Ask' : 'Search'}</button>
      </form>
      {mode === 'ask' && !llm && <p className="muted small">No LLM detected — Ask will return ranked results without a written answer.</p>}
      {!res && !busy && (
        <div className="examples">
          {EXAMPLES[mode].map((e) => <button key={e} className="chip" onClick={() => run(e)}>{e}</button>)}
        </div>
      )}
      </div>
      {err && <div className="error">{err}</div>}

      {res?.kind === 'ask' && (
        <div className="answer card">
          <div className="ahead">
            <span className="badge">{res.mode === 'llm' ? 'Tracebase' : 'Results'}</span>
            <span className="muted small">{res.ms} ms · {res.sources.length} sources</span>
          </div>
          <Markdown text={res.answer} sources={res.sources} onOpen={onOpen} />
          <h4>Sources</h4>
          <Results items={res.sources} onOpen={onOpen} compact />
        </div>
      )}
      {res?.kind === 'search' && (
        <>
          <p className="muted small">{res.results.length} results in {res.took_ms} ms</p>
          <Results items={res.results} onOpen={onOpen} />
        </>
      )}
      {res?.kind === 'grep' && (
        <>
          <p className="muted small">{res.error || `${res.results.length} matches in ${res.took_ms} ms`}</p>
          <div className="card list">
            {res.results.map((r, i) => (
              <button key={i} className="row" onClick={() => onOpen({ repo: r.repo, path: r.path, line: r.line })}>
                <span className="loc"><b>{r.repo}</b> {r.path}:{r.line}</span>
                <code className="snip">{r.text}</code>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function Results({ items, onOpen, compact }) {
  if (!items.length) return <p className="muted">No matches.</p>
  return (
    <div className="results">
      {items.map((r, i) => (
        <button key={i} className="result card" onClick={() => onOpen({ repo: r.repo, path: r.path, line: r.lines[0], end: r.lines[1] })}>
          <div className="rhead">
            <span className="loc"><b>{r.repo}</b> · {r.path} : {r.lines[0]}–{r.lines[1]}</span>
            <span className="score" title="relevance">{r.score.toFixed(2)}</span>
          </div>
          <div className="sym"><span className="kind">{r.kind}</span> {r.symbol} <span className="muted">· {r.language}</span></div>
          {!compact && <pre className="preview">{r.preview}</pre>}
        </button>
      ))}
    </div>
  )
}
