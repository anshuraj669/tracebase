import { useEffect, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'

export default function ReposPage({ onOpen, stats, onReindex }) {
  const [repos, setRepos] = useState([])
  const [sel, setSel] = useState(null)
  const [tree, setTree] = useState([])
  const [busy, setBusy] = useState(false)

  const load = () => api.repos().then(setRepos)
  useEffect(() => { load() }, [stats?.indexer?.last_run])
  useEffect(() => { if (sel) api.tree(sel).then(setTree) }, [sel])

  const reindex = async () => {
    setBusy(true)
    await api.reindex()
    setTimeout(() => { setBusy(false); onReindex(); load() }, 1500)
  }

  return (
    <div>
      <div className="row-between">
        <div>
          <p className="muted lead">Folders, Git clones and .zip archives found in the repos folder. Re-indexed automatically when files change.</p>
        </div>
        <button className="primary btn" onClick={reindex} disabled={busy || stats?.indexer?.running}>{busy || stats?.indexer?.running ? 'Indexing…' : 'Re-index now'}</button>
      </div>
      {stats && (
        <div className="kpis">
          <div className="card stat"><span className="ic t1"><Icon name="repo" size={20} /></span><div><b>{stats.repos}</b><span>Repositories</span></div></div>
          <div className="card stat"><span className="ic t2"><Icon name="file" size={20} /></span><div><b>{stats.files}</b><span>Source files</span></div></div>
          <div className="card stat"><span className="ic t3"><Icon name="code" size={20} /></span><div><b>{stats.chunks}</b><span>Functions &amp; classes</span></div></div>
          <div className="card stat"><span className="ic t4"><Icon name="key" size={20} /></span><div><b className={stats.secrets_high ? 'bad' : ''}>{stats.secrets}</b><span>Possible secrets</span></div></div>
        </div>
      )}
      <div className="repo-grid">
        {repos.map((r) => (
          <button key={r.name} className={`card repo ${sel === r.name ? 'on' : ''}`} onClick={() => setSel(sel === r.name ? null : r.name)}>
            <div className="rhead"><b>{r.name}</b><span className={`kindtag ${r.kind}`}>{r.kind}</span></div>
            <div className="muted small">{r.files} files · {r.chunks} chunks · indexed {r.indexed_at?.slice(0, 16).replace('T', ' ')}</div>
            <div className="langs">
              {Object.entries(r.languages).slice(0, 5).map(([l, n]) => <span key={l} className="chip sm">{l} {n}</span>)}
            </div>
          </button>
        ))}
        {!repos.length && <p className="muted">No repositories indexed yet. Point TRACEBASE_REPOS_DIR at your code folder.</p>}
      </div>
      {sel && (
        <div className="card list">
          <h3 className="pad">{sel}</h3>
          {tree.map((f) => (
            <button key={f.path} className="row" onClick={() => onOpen({ repo: sel, path: f.path })}>
              <span className="loc">{f.path}</span>
              <span className="muted small">{f.language} · {(f.size / 1024).toFixed(1)} KB</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
