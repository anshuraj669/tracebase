import { useEffect, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'

const size = (n) => {
  for (const u of ['B', 'KB', 'MB', 'GB']) { if (n < 1024) return `${n.toFixed(0)} ${u}`; n /= 1024 }
  return `${n.toFixed(1)} TB`
}
const deleted = (r) => Object.entries(r.deleted || r.would_delete || {}).filter(([, v]) => v)
  .map(([k, v]) => `${v} ${k.replace('_', ' ')}`).join(', ') || 'nothing'

export default function DataPage() {
  const [s, setS] = useState(null)
  const [err, setErr] = useState(null)
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [match, setMatch] = useState('')
  const [repo, setRepo] = useState('')
  const load = () => api.dataSummary().then(setS).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const run = async (fn, confirmText) => {
    if (confirmText && !confirm(confirmText)) return
    setBusy(true); setMsg(null)
    try { setMsg(await fn()) } catch (e) { setMsg(`Error: ${e.message}`) }
    setBusy(false); load()
  }

  if (err) return <p className="error">{err === 'admin only' ? 'Only an admin can see and manage stored data.' : err}</p>
  if (!s) return <p className="muted">Loading…</p>
  const c = s.counts
  const purge = async () => {
    const preview = await api.purge(match, true)
    if (!Object.values(preview.would_delete).some(Boolean)) return 'Nothing stored contains that text.'
    if (!confirm(`Delete ${deleted(preview)} containing “${match}”? This cannot be undone.`)) return 'Cancelled.'
    const r = await api.purge(match)
    setMatch('')
    return `✓ Deleted ${deleted(r)}.`
  }

  return (
    <div>
      <p className="muted lead">Everything Tracebase stores lives in one database on this server. Here you can see it, export it and delete it.</p>
      <div className="kpis">
        <div className="card stat"><span className="ic t1"><Icon name="backup" size={20} /></span><div><b>{size(s.database.size_bytes)}</b><span>{s.database.encrypted ? 'Encrypted at rest' : <span className="bad">Not encrypted: set TRACEBASE_DB_KEY</span>}</span></div></div>
        <div className="card stat"><span className="ic t2"><Icon name="code" size={20} /></span><div><b>{c.repos}</b><span>{c.files} files · {c.chunks} code chunks</span></div></div>
        <div className="card stat"><span className="ic t3"><Icon name="logs" size={20} /></span><div><b>{c.log_events}</b><span>log lines · {c.error_groups} errors · {c.alerts} alerts</span></div></div>
        <div className="card stat"><span className="ic t4"><Icon name="shield" size={20} /></span><div><b className={s.redaction.enabled ? '' : 'bad'}>{s.redaction.enabled ? 'On' : 'Off'}</b><span>Redaction of personal data in logs</span></div></div>
      </div>

      <div className="card panel">
        <div className="phead"><h3>Retention</h3><button disabled={busy} onClick={() => run(async () => `✓ Deleted ${deleted(await api.dataPrune())}.`)}>Apply now</button></div>
        <p className="muted small">Older records are deleted at startup and every night. Change with <code>TRACEBASE_*_RETENTION_DAYS</code> (0 = keep forever).</p>
        <p className="small">{Object.entries(s.retention_days).map(([k, v]) => `${k.replace('_', ' ')}: ${v ? `${v} days` : 'forever'}`).join(' · ')}
          {s.logs.oldest && <span className="muted"> · logs stored from {s.logs.oldest.replace('T', ' ')} to {s.logs.newest.replace('T', ' ')}</span>}</p>
      </div>

      <div className="card panel" style={{ marginTop: 18 }}>
        <div className="phead"><h3>Erase a person or a value</h3></div>
        <p className="muted small">Deletes every log line, error and alert containing the text (a customer id, an order number…). Emails, IPs, card numbers and tokens are already removed before storage{s.redaction.enabled ? '' : ' (redaction is off!)'}.</p>
        <div className="send-row">
          <input value={match} onChange={(e) => setMatch(e.target.value)} placeholder="e.g. CUST-10442" aria-label="Text to erase" />
          <button disabled={busy || match.trim().length < 4} onClick={() => api.exportData(undefined, match.trim()).catch((e) => setMsg(e.message))}>Export</button>
          <button className="primary btn" disabled={busy || match.trim().length < 4} onClick={() => run(purge)}>Erase</button>
        </div>
      </div>

      <div className="card list" style={{ marginTop: 18 }}>
        <div className="pad phead"><h3>Log services</h3><button onClick={() => api.exportData().catch((e) => setMsg(e.message))}>Export all</button></div>
        {s.services.map((v) => (
          <div key={v.service} className="row">
            <b style={{ flex: 1 }}>{v.service}</b><span className="muted small">{v.events} log lines</span>
            <button onClick={() => api.exportData(v.service).catch((e) => setMsg(e.message))}>Export</button>
            <button disabled={busy} onClick={() => run(async () => `✓ Deleted ${deleted(await api.forgetService(v.service))} of ${v.service}.`,
              `Delete all logs, errors and alerts of ${v.service}?`)}>Delete</button>
          </div>
        ))}
        {!s.services.length && <p className="pad muted">No logs stored.</p>}
      </div>

      {(s.excluded.repos.length > 0 || s.excluded.services.length > 0) && (
        <div className="card list" style={{ marginTop: 18 }}>
          <div className="pad phead"><h3>Forgotten</h3></div>
          {s.excluded.repos.map((r) => (
            <div key={r} className="row"><b style={{ flex: 1 }}>{r}</b><span className="muted small">repo: not indexed or readable</span>
              <button disabled={busy} onClick={() => run(async () => { await api.restoreRepo(r); return `✓ ${r} will be indexed again.` })}>Restore</button></div>
          ))}
          {s.excluded.services.map((r) => (
            <div key={r} className="row"><b style={{ flex: 1 }}>{r}</b><span className="muted small">log service: new logs dropped</span>
              <button disabled={busy} onClick={() => run(async () => { await api.restoreService(r); return `✓ Logs for ${r} are accepted again.` })}>Restore</button></div>
          ))}
        </div>
      )}
      <div className="card panel" style={{ marginTop: 18 }}>
        <div className="phead"><h3>Forget a repository</h3></div>
        <p className="muted small">Deletes its code index, embeddings and secrets findings, and stops indexing it. The folder on disk is not touched.</p>
        <div className="send-row">
          <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="e.g. client-a/old-site" aria-label="Repository" />
          <button className="primary btn" disabled={busy || !repo.trim()} onClick={() => run(async () => {
            const r = await api.forgetRepo(repo.trim()); setRepo(''); return `✓ Forgot ${r.repo}: deleted ${deleted(r)}.`
          }, `Delete the index of ${repo.trim()} and stop indexing it?`)}>Forget</button>
        </div>
      </div>
      <p className="muted small" style={{ marginTop: 14 }}>Sent outside this server: alert webhook {s.outbound.alert_webhook ? 'on' : 'off'} · LLM {s.outbound.llm.provider}{s.outbound.llm.provider !== 'none' ? ` at ${s.outbound.llm.base_url}` : ''}.</p>
      {msg && <p className={msg.startsWith('Error') ? 'error' : 'tip'}>{msg}</p>}
    </div>
  )
}
