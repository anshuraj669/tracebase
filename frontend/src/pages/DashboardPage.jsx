import { useEffect, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'

const PALETTE = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)', 'var(--c6)']
const QUICK = ['Where did I implement JWT refresh tokens before?', 'function that resizes uploaded images', 'How do I send password reset emails?']

export default function DashboardPage({ stats, onOpen, onAsk }) {
  const [repos, setRepos] = useState([])
  const [secrets, setSecrets] = useState(null)
  useEffect(() => {
    api.repos().then(setRepos).catch(() => {})
    api.secrets().then(setSecrets).catch(() => {})
  }, [stats?.indexer?.last_run])

  const kpis = [
    { icon: 'repo', tone: 't1', value: stats?.repos, label: 'Repositories' },
    { icon: 'file', tone: 't2', value: stats?.files, label: 'Source files' },
    { icon: 'code', tone: 't3', value: stats?.chunks, label: 'Functions & classes' },
    { icon: 'key', tone: 't4', value: stats?.secrets, label: 'Possible secrets' },
  ]

  return (
    <div className="dash">
      <div className="kpi-row">
        {kpis.map((k) => (
          <div key={k.label} className="card stat">
            <span className={`ic ${k.tone}`}><Icon name={k.icon} size={20} /></span>
            <div>
              <b>{k.value ?? '–'}</b>
              <span>{k.label}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="dash-grid">
        <div className="card panel">
          <div className="phead"><h3>Repositories</h3><a href="#repos" className="link">View all <Icon name="arrow" size={14} /></a></div>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Name</th><th>Type</th><th>Files</th><th>Chunks</th><th>Top language</th></tr></thead>
              <tbody>
                {repos.map((r) => {
                  const top = Object.entries(r.languages || {}).sort((a, b) => b[1] - a[1])[0]
                  return (
                    <tr key={r.name}>
                      <td className="strong">{r.name}</td>
                      <td><span className={`kindtag ${r.kind}`}>{r.kind}</span></td>
                      <td>{r.files}</td>
                      <td><span className="num-pill">{r.chunks}</span></td>
                      <td className="muted">{top?.[0] || '–'}</td>
                    </tr>
                  )
                })}
                {!repos.length && <tr><td colSpan="5" className="muted">No repositories indexed yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card panel">
          <div className="phead"><h3>Languages</h3></div>
          <Donut items={(stats?.languages || []).slice(0, 6)} total={stats?.files} />
        </div>

        <div className="card panel">
          <div className="phead"><h3>Ask your code</h3></div>
          <p className="muted small">Answers come from your own repos, with file:line citations.</p>
          <div className="quick">
            {QUICK.map((q) => (
              <button key={q} className="quick-item" onClick={() => onAsk(q)}>
                <Icon name="spark" size={16} /> <span>{q}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="card panel">
          <div className="phead"><h3>Secrets scan</h3><a href="#secrets" className="link">View all <Icon name="arrow" size={14} /></a></div>
          {!secrets && <p className="muted">Loading…</p>}
          {secrets?.findings.slice(0, 4).map((f, i) => (
            <button key={i} className="mini-row" onClick={() => onOpen({ repo: f.repo, path: f.path, line: f.line })}>
              <span className={`sev ${f.severity.toLowerCase()}`}>{f.severity}</span>
              <span className="mini-main">
                <b>{f.rule}</b>
                <span className="muted small loc">{f.repo}/{f.path}:{f.line}</span>
              </span>
            </button>
          ))}
          {secrets && !secrets.findings.length && <p className="muted">No secrets found.</p>}
        </div>
      </div>
    </div>
  )
}

function Donut({ items, total }) {
  const sum = items.reduce((s, x) => s + x.n, 0) || 1
  const R = 54, C = 2 * Math.PI * R
  let off = 0
  return (
    <div className="donut">
      <svg viewBox="0 0 140 140" width="168" height="168">
        <circle cx="70" cy="70" r={R} fill="none" stroke="var(--track)" strokeWidth="16" />
        {items.map((x, i) => {
          const len = (x.n / sum) * C
          const el = (
            <circle key={x.language} cx="70" cy="70" r={R} fill="none" stroke={PALETTE[i % PALETTE.length]} strokeWidth="16"
              strokeDasharray={`${Math.max(len - 2, 0)} ${C}`} strokeDashoffset={-off} transform="rotate(-90 70 70)" strokeLinecap="butt" />
          )
          off += len
          return el
        })}
        <text x="70" y="68" textAnchor="middle" className="donut-n">{total ?? sum}</text>
        <text x="70" y="86" textAnchor="middle" className="donut-l">files</text>
      </svg>
      <ul className="legend">
        {items.map((x, i) => (
          <li key={x.language}><i style={{ background: PALETTE[i % PALETTE.length] }} />{x.language}<span>{x.n}</span></li>
        ))}
      </ul>
    </div>
  )
}
