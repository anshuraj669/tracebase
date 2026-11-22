import { useEffect, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'

export default function SecretsPage({ onOpen }) {
  const [data, setData] = useState(null)
  useEffect(() => { api.secrets().then(setData) }, [])
  if (!data) return <p className="muted">Loading…</p>
  const high = data.findings.filter((f) => f.severity === 'HIGH').length
  return (
    <div>
      <p className="muted lead">Leaked API keys, private keys, hard-coded passwords and committed .env files, found while indexing. Values are masked.</p>
      <div className="kpis">
        <div className="card stat"><span className="ic t4"><Icon name="shield" size={20} /></span><div><b className={high ? 'bad' : ''}>{high}</b><span>High severity</span></div></div>
        <div className="card stat"><span className="ic t3"><Icon name="key" size={20} /></span><div><b>{data.findings.length - high}</b><span>Medium severity</span></div></div>
      </div>
      <div className="card list">
        {data.findings.map((f, i) => (
          <button key={i} className="row" onClick={() => onOpen({ repo: f.repo, path: f.path, line: f.line })}>
            <span className={`sev ${f.severity.toLowerCase()}`}>{f.severity}</span>
            <span className="loc"><b>{f.repo}</b> {f.path}:{f.line}</span>
            <span className="muted small">{f.rule}</span>
            <code className="snip">{f.preview}</code>
          </button>
        ))}
        {!data.findings.length && <p className="pad muted">No secrets found. ✓</p>}
      </div>
      {data.findings.length > 0 && <p className="tip">→ {data.findings.length} findings. {data.suggestion}</p>}
    </div>
  )
}
