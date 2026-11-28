import { useEffect, useState } from 'react'
import { api } from '../api'

export default function TeamPage({ me, googleOn }) {
  const [items, setItems] = useState(null)
  const [err, setErr] = useState(null)
  const [token, setToken] = useState(null) // shown once after inviting
  const load = () => api.members().then(setItems).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const remove = async (m) => {
    if (!confirm(`Remove ${m.name}? Their token and Google sign-in stop working at once.`)) return
    try { await api.removeMember(m.name); load() } catch (e) { setErr(e.message) }
  }
  const editEmail = async (m) => {
    const email = prompt(`Google account for ${m.name} (empty = no Google sign-in):`, m.email || '')
    if (email === null) return
    try { await api.setMemberEmail(m.name, email.trim()); load() } catch (e) { setErr(e.message) }
  }
  const editRepos = async (m) => {
    const v = prompt(`What can ${m.name} see? Repos and log services, comma-separated (patterns like freelance/* work; * = everything):`, m.repos.join(', '))
    if (v === null) return
    try { await api.setMemberRepos(m.name, v.split(',').map((x) => x.trim()).filter(Boolean)); load() } catch (e) { setErr(e.message) }
  }

  return (
    <div>
      <p className="muted lead">Who can open this Tracebase. Invite people by their Google account so they can choose <b>Continue with Google</b>, or give them a token. A member only sees the repos and log services you allow.</p>
      {!googleOn && <div className="card panel note-card small">Google sign-in is off. To turn it on, set <code>TRACEBASE_GOOGLE_CLIENT_ID</code> (and <code>TRACEBASE_ADMIN_EMAILS</code> for admins) on the server: see the deploy guide.</div>}
      <Invite onDone={(name, tok) => { setToken({ name, tok }); load() }} />
      {token && (
        <div className="card panel">
          <p className="small"><b>{token.name}</b> was added.{googleOn ? ' They can sign in with Google now.' : ''} Their token, if they prefer the CLI or a token (shown once):</p>
          <div className="copyline"><code>{token.tok}</code><button onClick={() => navigator.clipboard?.writeText(token.tok)}>Copy</button></div>
        </div>
      )}
      {err && <div className="error">{err}</div>}
      <div className="card list">
        <div className="phead pad"><h3>Members</h3><span className="muted small">{items ? items.length : '…'}</span></div>
        {items?.map((m) => (
          <div key={m.name} className="row member-row">
            <div className="err-main">
              <b>{m.name} {m.role === 'admin' && <span className="badge">admin</span>} {me?.name === m.name && <span className="muted small">(you)</span>}</b>
              <span className="muted small">{m.email || 'no Google account'} · sees {m.repos.join(', ')} · {m.last_seen ? `last seen ${m.last_seen.slice(0, 16)}` : 'never signed in'}</span>
            </div>
            <button onClick={() => editEmail(m)}>Google account</button>
            <button onClick={() => editRepos(m)}>Access</button>
            {me?.name !== m.name && <button className="danger-link" onClick={() => remove(m)}>Remove</button>}
          </div>
        ))}
        {items && !items.length && <p className="muted pad">Only the admin token so far. Invite someone above.</p>}
      </div>
    </div>
  )
}

function Invite({ onDone }) {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [repos, setRepos] = useState('*')
  const [admin, setAdmin] = useState(false)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const guess = (name || email.split('@')[0] || '').toLowerCase().replace(/[^a-z0-9._-]+/g, '-').slice(0, 40)
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const r = await api.addMember({ name: guess, email: email.trim() || null, role: admin ? 'admin' : 'member',
        repos: repos.split(',').map((x) => x.trim()).filter(Boolean) })
      setEmail(''); setName(''); setRepos('*'); setAdmin(false)
      onDone(r.name, r.token)
    } catch (e2) { setErr(e2.message) } finally { setBusy(false) }
  }
  return (
    <form className="card panel invite" onSubmit={submit}>
      <div className="phead"><h3>Invite someone</h3></div>
      <div className="invite-grid">
        <label className="fld"><span>Google account (email)</span><input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="priya@company.com" /></label>
        <label className="fld"><span>Name in Tracebase</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder={guess || 'priya'} /></label>
        <label className="fld"><span>Can see <span className="muted small">(repos and services, comma-separated)</span></span><input value={repos} onChange={(e) => setRepos(e.target.value)} placeholder="meetsnap-web, freelance/*" /></label>
      </div>
      <label className="check"><input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} /> Admin (can invite people, connect projects and change flags)</label>
      {err && <div className="error">{err}</div>}
      <div><button className="primary btn" disabled={busy || !guess}>{busy ? 'Adding…' : 'Add member'}</button></div>
    </form>
  )
}
