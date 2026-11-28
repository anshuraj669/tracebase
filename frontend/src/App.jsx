import { useEffect, useRef, useState } from 'react'
import { api, auth } from './api'
import DashboardPage from './pages/DashboardPage'
import SearchPage from './pages/SearchPage'
import ReposPage from './pages/ReposPage'
import SecretsPage from './pages/SecretsPage'
import LogsPage from './pages/LogsPage'
import DataPage from './pages/DataPage'
import ProjectsPage from './pages/ProjectsPage'
import TeamPage from './pages/TeamPage'
import CodeViewer from './components/CodeViewer'
import Icon, { Logo } from './components/Icon'

const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard', title: 'Dashboard' },
  { id: 'search', label: 'Search & Ask', icon: 'search', title: 'Search & Ask' },
  { id: 'repos', label: 'Repositories', icon: 'repo', title: 'Repositories' },
  { id: 'logs', label: 'Log Detective', icon: 'logs', title: 'Log Detective' },
  { id: 'projects', label: 'Projects', icon: 'plug', title: 'Connect a project' },
  { id: 'secrets', label: 'Secrets scan', icon: 'shield', title: 'Secrets scan' },
  { id: 'data', label: 'Data & privacy', icon: 'backup', title: 'Data & privacy', admin: true },
  { id: 'team', label: 'Team', icon: 'users', title: 'Team', admin: true, team: true },
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
  const [authCfg, setAuthCfg] = useState(null)
  useEffect(() => { if (health?.team) api.me().then(setMe).catch(() => {}) }, [health?.team])
  useEffect(() => { api.authConfig().then(setAuthCfg).catch(() => {}) }, [])

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
          {NAV.filter((n) => (!n.admin || !me || me.role === 'admin') && (!n.team || health?.team)).map((n) => (
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
              <button className="link-btn" onClick={async () => { await api.signOut(); auth.clear(); location.reload() }}>Sign out</button>
            </>
          ) : (
            <>
              <b>Private by design</b>
              <span>Your code and logs are indexed and explained on this machine. Personal data in logs is redacted before it is stored.</span>
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
            {page.id === 'data' && <DataPage />}
            {page.id === 'projects' && <ProjectsPage selected={sub} me={me} />}
            {page.id === 'team' && <TeamPage me={me} googleOn={!!authCfg?.google_client_id} />}
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
  const [cfg, setCfg] = useState(null)
  const [showToken, setShowToken] = useState(false)
  useEffect(() => { api.authConfig().then(setCfg).catch(() => setCfg({})) }, [])
  const done = (t) => { auth.set(t); location.hash = location.hash || 'dashboard'; location.reload() }
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      await api.me(token.trim())
      done(token.trim())
    } catch (e2) {
      setErr('That token was not accepted. Ask your Tracebase admin for a new one.')
      setBusy(false)
    }
  }
  const google = cfg?.google_client_id
  return (
    <div className="signin">
      <div className="card signin-card">
        <div className="side-brand"><Logo /> <span>Tracebase</span></div>
        <h2>Sign in to your team's Tracebase</h2>
        {google && (
          <>
            <p className="muted">Use the Google account your admin invited.</p>
            <GoogleButton clientId={google} onToken={done} onError={setErr} />
            {!showToken && <button type="button" className="link-btn" onClick={() => setShowToken(true)}>Use an access token instead</button>}
          </>
        )}
        {(!google || showToken) && (
          <form className="signin-form" onSubmit={submit}>
            <p className="muted">{google ? 'Or p' : 'P'}aste the token your admin gave you (it starts with <code>tb_</code>). It stays in this browser.</p>
            <input autoFocus={!google} type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="tb_…" aria-label="Access token" />
            <button className="primary btn" disabled={busy || !token.trim()}>{busy ? 'Checking…' : 'Sign in'}</button>
          </form>
        )}
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}

// Google's own button (Google Identity Services): it hands us a signed ID token, which the server verifies
function GoogleButton({ clientId, onToken, onError }) {
  const ref = useRef(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const init = () => {
      window.google.accounts.id.initialize({
        client_id: clientId,
        ux_mode: 'popup',
        callback: async (r) => {
          setBusy(true)
          onError(null)
          try {
            onToken((await api.googleSignIn(r.credential)).token)
          } catch (e) {
            onError(e.message)
            setBusy(false)
          }
        },
      })
      window.google.accounts.id.renderButton(ref.current, { theme: 'outline', size: 'large', text: 'continue_with', shape: 'pill', width: 320 })
    }
    if (window.google?.accounts?.id) return init()
    const s = document.createElement('script')
    s.src = 'https://accounts.google.com/gsi/client'
    s.async = true
    s.onload = init
    s.onerror = () => onError('Could not load Google sign-in (offline, or blocked by an extension). Use an access token instead.')
    document.head.appendChild(s)
  }, [clientId])
  return <div className="gbtn">{busy ? <p className="muted small">Signing in…</p> : <div ref={ref} />}</div>
}
