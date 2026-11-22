import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import Markdown from './Markdown'
import Icon from './Icon'

export default function CodeViewer({ loc, onClose, llm }) {
  const [file, setFile] = useState(null)
  const [err, setErr] = useState(null)
  const [sel, setSel] = useState(loc.line || null)
  const [explain, setExplain] = useState(null)
  const [blame, setBlame] = useState(null)
  const [busy, setBusy] = useState('')
  const body = useRef(null)

  useEffect(() => {
    api.file(loc.repo, loc.path).then(setFile).catch((e) => setErr(e.message))
  }, [loc.repo, loc.path])

  useEffect(() => {
    setSel(loc.line || null)
    setExplain(null)
    setBlame(null)
  }, [loc.line])

  useEffect(() => {
    if (!file || !loc.line) return
    const box = body.current
    const el = box?.querySelector(`[data-n="${Math.max(1, loc.line - 3)}"]`)
    if (box && el) box.scrollTop = el.offsetTop - box.offsetTop  // scroll only the code pane, not the page
  }, [file, loc.line])

  const doExplain = async () => {
    setBusy('explain')
    setExplain(null)
    try {
      setExplain(await api.explain(loc.repo, loc.path, sel))
    } catch (e) {
      setExplain({ note: e.message })
    }
    setBusy('')
  }
  const doBlame = async () => {
    setBusy('blame')
    try {
      setBlame(await api.history(loc.repo, loc.path, sel))
    } catch (e) {
      setBlame({ error: e.message })
    }
    setBusy('')
  }

  const inRange = (n) => (explain?.start ? n >= explain.start && n <= explain.end : loc.line && n >= loc.line && n <= (loc.end || loc.line))

  return (
    <aside className="viewer">
      <div className="vhead">
        <div className="vpath">
          <span className="repo">{loc.repo}</span>
          <span className="path">{loc.path}</span>
          {file && <span className="muted"> · {file.language || 'text'} · {file.total_lines} lines</span>}
        </div>
        <button className="ghost" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
      </div>
      <div className="vtools">
        <span className="muted">{sel ? `Line ${sel} selected` : 'Click a line number to select'}</span>
        <button onClick={doExplain} disabled={!!busy || !llm} title={llm ? '' : 'Needs a local LLM'}>
          {busy === 'explain' ? 'Explaining…' : sel ? 'Explain this function' : 'Explain file'}
        </button>
        <button onClick={doBlame} disabled={!!busy || !sel}>
          {busy === 'blame' ? 'Loading…' : 'Who changed this line?'}
        </button>
      </div>
      {explain && (
        <div className="note">
          {explain.symbol && <div className="muted">`{explain.symbol}` · lines {explain.start}–{explain.end}</div>}
          {explain.explanation ? <Markdown text={explain.explanation} /> : <p className="muted">{explain.note}</p>}
        </div>
      )}
      {blame && (
        <div className="note">
          {blame.error ? (
            <p className="muted">{blame.error}</p>
          ) : (
            blame.commits?.slice(0, 1).map((c) => (
              <div key={c.sha}>
                <div>
                  <b className="mono">{c.sha}</b> · {c.author} · {c.date.slice(0, 16)}
                </div>
                <div>“{c.message}”</div>
                {blame.diff && (
                  <pre className="diff">
                    {blame.diff.split('\n').filter((l) => !l.startsWith('diff --git') && !l.startsWith('index ')).map((l, i) => (
                      <div key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'add' : l.startsWith('-') && !l.startsWith('---') ? 'del' : ''}>{l || ' '}</div>
                    ))}
                  </pre>
                )}
              </div>
            ))
          )}
        </div>
      )}
      <div className="code" ref={body}>
        {err && <p className="muted pad">{err}</p>}
        {!file && !err && <p className="muted pad">Loading…</p>}
        {file?.lines.map((l) => (
          <div key={l.n} data-n={l.n} className={`ln ${inRange(l.n) ? 'hl' : ''} ${sel === l.n ? 'sel' : ''}`}>
            <button className="n" onClick={() => { setSel(l.n); setBlame(null) }}>{l.n}</button>
            <code>{l.text || ' '}</code>
          </div>
        ))}
      </div>
    </aside>
  )
}
