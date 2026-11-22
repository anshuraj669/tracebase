import { useEffect, useState } from 'react'
import { api, auth } from './api'
import DashboardPage from './pages/DashboardPage'
import SearchPage from './pages/SearchPage'
import ReposPage from './pages/ReposPage'
import SecretsPage from './pages/SecretsPage'
import LogsPage from './pages/LogsPage'
import CodeViewer from './components/CodeViewer'
import Icon, { Logo } from './components/Icon'

const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard', title: 'Dashboard' },
  { id: 'search', label: 'Search & Ask', icon: 'search', title: 'Search & Ask' },
  { id: 'repos', label: 'Repositories', icon: 'repo', title: 'Repositories' },
  { id: 'logs', label: 'Log Detective', icon: 'logs', title: 'Log Detective' },
  { id: 'secrets', label: 'Secrets scan', icon: 'shield', title: 'Secrets scan' },
]
const SOON = [
  { label: 'Backups', icon: 'backup' },
]

function useHash() {
  const get = () => location.hash.slice(1) || 'dashboard'
  const [hash, setHash] = useState(get)
  useEffect(() => {
    const on = () => setHash(get())
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, [])
  return hash
}

export default function App() {
  const tab = useHash()
  const [health, setHealth] = useState(null)
  const [stats, setStats] = useState(null)
  const [open, setOpen] = useState(null) // {repo, path, line, end}
  const [pending, setPending] = useState(null) // question sent from the dashboard

  const [signin, setSignin] = useState(false)
  const [me, setMe] = useState(null)
  useEffect(() => {
    const on = () => setSignin(true)
    addEventListener('tracebase:signin', on)
    return () => removeEventListener('tracebase:signin', on)
  }, [])
  useEffect(() => { if (health?.team) api.me().then(setMe).catch(() => {}) }, [health?.team])

  const refresh = () => {
    api.health().then(setHealth).catch(() => setHealth(null))
    api.stats().then(setStats).catch(() => {})
  }
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 10000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => { setOpen(null) }, [tab.split('/')[0]])

  const ask = (q) => { setPending({ q, at: Date.now() }); location.hash = 'search' }
  const [tabId, sub] = tab.split('/') // #logs/E-5
  const page = NAV.find((n) => n.id === tabId) || NAV[0]
  const llmOk = health?.llm?.available

  if (signin) return <SignIn />

  return (
    <div className="shell">
      <aside className="side">
        <div className="side-brand"><Logo /> <span>Tracebase</span></div>
        <nav className="side-nav">
          {NAV.map((n) => (
            <a key={n.id} href={`#${n.id}`} className={page.id === n.id ? 'active' : ''}>
              <Icon name={n.icon} /> <span>{n.label}</span>
              {n.id === 'logs' && stats?.open_errors > 0 && <span className="nav-count">{stats.open_errors}</span>}
            </a>
          ))}
          <div className="side-sep">Coming soon</div>
          {SOON.map((s) => (
            <span key={s.label} className="soon" title="Coming in a later phase">
              <Icon name={s.icon} /> <span>{s.label}</span>
            </span>
          ))}
        </nav>
        <div className="side-foot">
          {me ? (
            <>
              <b>Team server</b>
              <span>Signed in as <strong>{me.name}</strong> ({me.role}) · repos: {me.repos.join(', ')}</span>
              <button className="link-btn" onClick={() => { auth.clear(); location.reload() }}>Sign out</button>
            </>
          ) : (
            <>
              <b>Private by design</b>
              <span>Your code and logs are indexed and explained on this machine. Nothing leaves your network.</span>
            </>
          )}
        </div>
      </aside>

      <main className={`content ${open ? 'split' : ''}`}>
        <div className="col">
        <header className="topbar">
          <h1>{page.title}</h1>
          <div className="status">
            {stats?.indexer?.running && <span className="pill"><span className="spin" /> Indexing…</span>}
            <span className="pill" title="Embedding model">{health?.embeddings ? `Embeddings: ${health.embeddings}` : '–'}</span>
            <span className={`pill ${llmOk ? 'ok' : 'off'}`} title={health ? `${health.llm.provider} · ${health.llm.model}` : 'API offline'}>
              <i className="dot" /> {health ? (llmOk ? `LLM: ${health.llm.model}` : 'LLM offline') : 'API offline'}
            </span>
          </div>
        </header>
          <section className="page">
            {page.id === 'dashboard' && <DashboardPage stats={stats} onOpen={setOpen} onAsk={ask} />}
            {page.id === 'search' && <SearchPage onOpen={setOpen} llm={llmOk} pending={pending} />}
            {page.id === 'repos' && <ReposPage onOpen={setOpen} stats={stats} onReindex={refresh} />}
            {page.id === 'logs' && <LogsPage onOpen={setOpen} stats={stats} selected={sub} />}
            {page.id === 'secrets' && <SecretsPage onOpen={setOpen} />}
          </section>
        </div>
        {open && <CodeViewer key={`${open.repo}/${open.path}`} loc={open} onClose={() => setOpen(null)} llm={llmOk} />}
      </main>
    </div>
  )
}

function SignIn() {
  const [token, setToken] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      await api.me(token.trim())
      auth.set(token.trim())
      location.reload()
    } catch (e2) {
      setErr('That token was not accepted. Ask your Tracebase admin for a new one.')
      setBusy(false)
    }
  }
  return (
    <div className="signin">
      <form className="card signin-card" onSubmit={submit}>
        <div className="side-brand"><Logo /> <span>Tracebase</span></div>
        <h2>Sign in to your team's Tracebase</h2>
        <p className="muted">Paste the token your admin gave you (it starts with <code>tb_</code>). It stays in this browser.</p>
        <input autoFocus type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="tb_…" aria-label="Access token" />
        {err && <div className="error">{err}</div>}
        <button className="primary" disabled={busy || !token.trim()}>{busy ? 'Checking…' : 'Sign in'}</button>
      </form>
    </div>
  )
}
