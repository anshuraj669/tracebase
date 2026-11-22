import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'

const RANGES = [
  { v: 'last night', l: 'Since last night' },
  { v: '24h', l: 'Last 24 hours' },
  { v: '7d', l: 'Last 7 days' },
  { v: '', l: 'Any time' },
]
const STATUS = ['new', 'regressed', 'known', 'resolved', 'ignored']
const when = (ts) => (ts || '').replace('T', ' ').slice(5, 16)

export default function LogsPage({ onOpen, stats, selected }) {
  const [since, setSince] = useState('last night')
  const [service, setService] = useState('')
  const [q, setQ] = useState('')
  const [groups, setGroups] = useState(null)
  const [services, setServices] = useState([])
  const [timeline, setTimeline] = useState([])
  const [alerts, setAlerts] = useState([])
  const [err, setErr] = useState(null)
  const [tick, setTick] = useState(0)
  const reload = () => setTick((t) => t + 1)

  useEffect(() => {
    api.logServices().then(setServices).catch(() => {})
    api.alerts(true).then(setAlerts).catch(() => {})
  }, [tick])
  useEffect(() => {
    setErr(null)
    api.errors({ since, service, q }).then(setGroups).catch((e) => setErr(e.message))
    api.logTimeline(service).then(setTimeline).catch(() => setTimeline([]))
  }, [since, service, q, tick])

  const open = (code) => { location.hash = code ? `logs/${code}` : 'logs' }
  const spiking = (groups || []).filter((g) => g.spike).length

  return (
    <div>
      <p className="muted lead">Your production logs, grouped: thousands of identical errors become one line. Pick one to see why it happened, the code that failed and the commit that changed it.</p>
      <div className="kpis">
        <Kpi icon="logs" tone="t4" n={stats?.open_errors ?? '–'} label="Open error types" bad={stats?.open_errors > 0} />
        <Kpi icon="spark" tone="t3" n={spiking} label="Spiking now" bad={spiking > 0} />
        <Kpi icon="file" tone="t2" n={(stats?.log_events ?? 0).toLocaleString()} label="Log lines collected" />
        <Kpi icon="shield" tone="t1" n={stats?.unread_alerts ?? '–'} label="Unread alerts" />
      </div>

      {selected ? (
        <ErrorDetail code={selected} onBack={() => open(null)} onOpen={onOpen} onChange={reload} />
      ) : (
        <>
          <div className="card panel">
            <div className="phead">
              <h3>Errors per 10 minutes {service && <span className="muted small">· {service}</span>}</h3>
              <span className="muted small">last 48h of logs</span>
            </div>
            <Timeline rows={timeline} />
          </div>

          <div className="log-filters">
            <select value={since} onChange={(e) => setSince(e.target.value)} aria-label="Time range">
              {RANGES.map((r) => <option key={r.v} value={r.v}>{r.l}</option>)}
            </select>
            <select value={service} onChange={(e) => setService(e.target.value)} aria-label="Service">
              <option value="">All services</option>
              {services.map((s) => <option key={s.service} value={s.service}>{s.service} ({s.errors || 0})</option>)}
            </select>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by message…" aria-label="Filter by message" />
          </div>

          <div className="card list">
            {err && <p className="pad error">{err}</p>}
            {!groups && !err && <p className="pad muted">Loading…</p>}
            {groups?.map((g) => (
              <button key={g.id} className="row err-row" onClick={() => open(g.code)}>
                <span className="err-code">{g.code}</span>
                <span className="err-main">
                  <b>{g.title}</b>
                  <span className="muted small">{g.service} · first {when(g.first_seen)} · last {when(g.last_seen)}</span>
                </span>
                {g.spike && <span className="badge spike">▲ spike</span>}
                <span className={`badge st-${g.status}`}>{g.status}</span>
                <span className="err-n">{(since ? g.window_count : g.count)?.toLocaleString()}×</span>
              </button>
            ))}
            {groups && !groups.length && <p className="pad muted">No errors {since ? `(${RANGES.find((r) => r.v === since)?.l.toLowerCase()})` : ''}. ✓</p>}
          </div>

          <div className="dash-grid log-bottom">
            <Alerts items={alerts} onOpen={open} onRead={() => api.readAlerts().then(reload)} />
            <SendLogs onDone={reload} />
          </div>
        </>
      )}
    </div>
  )
}

function Kpi({ icon, tone, n, label, bad }) {
  return (
    <div className="card stat">
      <span className={`ic ${tone}`}><Icon name={icon} size={20} /></span>
      <div><b className={bad ? 'bad' : ''}>{n}</b><span>{label}</span></div>
    </div>
  )
}

function Timeline({ rows }) {
  const max = Math.max(1, ...rows.map((r) => r.errors || 0))
  if (!rows.length) return <p className="muted small">No log lines yet. Drop log files into the logs folder, or send some below.</p>
  const w = 100 / rows.length
  return (
    <div className="tl">
      <svg viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label="Errors over time">
        {rows.map((r, i) => {
          const h = ((r.errors || 0) / max) * 38
          return <rect key={r.bucket} x={i * w} y={40 - h} width={Math.max(w * 0.8, 0.15)} height={h} className="tl-bar"><title>{`${r.bucket.replace('T', ' ')}: ${r.errors} errors`}</title></rect>
        })}
      </svg>
      <div className="tl-axis muted small"><span>{when(rows[0].bucket)}</span><span>peak {max}/10 min</span><span>{when(rows[rows.length - 1].bucket)}</span></div>
    </div>
  )
}

function ErrorDetail({ code, onBack, onOpen, onChange }) {
  const [g, setG] = useState(null)
  const [x, setX] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = (refresh = false) => {
    setBusy(true)
    setErr(null)
    api.logGroup(code).then(setG).catch((e) => setErr(e.message))
    api.explainError(code, refresh).then(setX).catch((e) => setErr(e.message)).finally(() => setBusy(false))
  }
  useEffect(() => { setX(null); load() }, [code])

  const setStatus = (s) => api.setErrorStatus(code, s).then(() => { api.logGroup(code).then(setG); onChange() })

  if (err) return <div className="card pad"><p className="error">{err}</p><button onClick={onBack}>← All errors</button></div>
  if (!g) return <p className="muted">Loading…</p>
  return (
    <div className="err-detail">
      <div className="err-head">
        <button className="link-btn" onClick={onBack}>← All errors</button>
        <h2><span className="err-code">{g.code}</span> {g.title}</h2>
        <p className="muted">
          <b>{g.service}</b> · {g.count.toLocaleString()} times · first {g.first_seen.replace('T', ' ')} · last {g.last_seen.replace('T', ' ')} · <span className={`badge st-${g.status}`}>{g.status}</span>
        </p>
        <div className="err-actions">
          {g.status !== 'resolved' && <button className="primary btn" onClick={() => setStatus('resolved')}>Mark resolved</button>}
          {STATUS.filter((s) => s !== g.status && s !== 'resolved' && s !== 'regressed').map((s) => (
            <button key={s} onClick={() => setStatus(s)}>{s === 'new' ? 'Reopen' : `Mark ${s}`}</button>
          ))}
          <button className="ghost" title="Investigate again" onClick={() => load(true)} disabled={busy}><Icon name="refresh" /></button>
        </div>
      </div>

      {!x ? (
        <div className="card pad"><span className="spin" /> Investigating: reading your code, git history and the other services' logs…</div>
      ) : (
        <div className="why-grid">
          <div className="card panel why">
            <h4>Why it happened</h4>
            <p>{x.cause}</p>
            <h4>Impact</h4>
            <p>{x.impact}</p>
            {x.fix?.length > 0 && (<>
              <h4>How to fix it</h4>
              <ol className="fix">{x.fix.map((f, i) => <li key={i}><Fixed text={f} /></li>)}</ol>
            </>)}
            <p className="muted small">Explained {x.source === 'llm' ? 'by your local LLM' : 'by built-in rules'} from your logs, code and git history, on this machine.</p>
          </div>
          <div className="why-side">
            {x.commit && (
              <div className="card panel commit">
                <h4>Last changed by</h4>
                <p><code className="sha">{x.commit.sha}</code> {x.commit.message}</p>
                <p className="muted small">{x.commit.author} · {x.commit.date}
                  {x.commit.minutes_before_first_error >= 0 && x.commit.minutes_before_first_error < 1440 &&
                    <b className="bad"> · {x.commit.minutes_before_first_error} min before the first error</b>}
                </p>
              </div>
            )}
            {x.events?.length > 0 && (
              <div className="card panel">
                <h4>Meanwhile in other services</h4>
                <ul className="events">
                  {x.events.map((e, i) => <li key={i}><span className="muted mono">{e.ts.slice(11, 19)}</span> <b>{e.service}</b> {e.message}</li>)}
                </ul>
              </div>
            )}
          </div>
          {x.code?.length > 0 && (
            <div className="card panel code-at">
              <h4>The code that failed</h4>
              {x.code.slice(0, 3).map((c) => (
                <div key={`${c.path}:${c.line}`} className="frame">
                  <button className="link-btn" onClick={() => onOpen({ repo: c.repo, path: c.path, line: c.line })}>
                    {c.repo}/{c.path}:{c.line} {c.func && <span className="muted">· {c.func}</span>} →
                  </button>
                  <pre>{c.snippet.map((l) => (
                    <div key={l.n} className={l.n === c.line ? 'hot' : ''}><span className="ln">{l.n}</span>{l.text}</div>
                  ))}</pre>
                </div>
              ))}
            </div>
          )}
          {g.samples?.length > 0 && (
            <div className="card panel code-at">
              <h4>Latest log lines</h4>
              {g.samples.slice(0, 3).map((s, i) => <pre key={i} className="sample">{s.ts.replace('T', ' ')}  {s.message.slice(0, 900)}</pre>)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// render `code` spans in fix steps
function Fixed({ text }) {
  const parts = useMemo(() => text.split(/(`[^`]+`)/g), [text])
  return parts.map((p, i) => (p.startsWith('`') ? <code key={i}>{p.slice(1, -1)}</code> : <span key={i}>{p}</span>))
}

function Alerts({ items, onOpen, onRead }) {
  return (
    <div className="card panel">
      <div className="phead"><h3>Alerts</h3>{items.length > 0 && <button className="link-btn" onClick={onRead}>Mark all read</button>}</div>
      {!items.length && <p className="muted small">No unread alerts. New error types, spikes and errors that come back after being resolved show up here.</p>}
      <ul className="alerts">
        {items.slice(0, 8).map((a) => (
          <li key={a.id}>
            <button className="mini-row" onClick={() => onOpen(a.link?.split('/logs/')[1] || null)}>
              <i className={`sev-dot ${a.severity}`} />
              <span><b>{a.title}</b><span className="muted small">{a.created_at?.slice(5, 16)} · {(a.body || '').split('\n')[0].slice(0, 90)}</span></span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function SendLogs({ onDone }) {
  const [text, setText] = useState('')
  const [service, setService] = useState('')
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const send = async () => {
    setBusy(true)
    try {
      const r = await api.ingest(text, service)
      setRes(`✓ ${r.events} lines, ${r.errors} errors${r.new_groups?.length ? `, new: ${r.new_groups.join(', ')}` : ''}`)
      setText('')
      onDone()
    } catch (e) {
      setRes(e.message)
    }
    setBusy(false)
  }
  const file = (e) => { const f = e.target.files?.[0]; if (f) { f.text().then(setText); if (!service) setService(f.name.split('.')[0]) } }
  return (
    <div className="card panel">
      <div className="phead"><h3>Send logs</h3><label className="link-btn">Choose file<input type="file" hidden onChange={file} /></label></div>
      <p className="muted small">Paste log lines (Laravel, Nginx, Node, Python, Java, JSON…). Files in the logs folder are picked up automatically; from a terminal: <code>tracebase ingest app.log</code>.</p>
      <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder="[2026-11-08 14:04:04] production.ERROR: …" aria-label="Log lines" />
      <div className="send-row">
        <input value={service} onChange={(e) => setService(e.target.value)} placeholder="service (optional)" aria-label="Service" />
        <button className="primary btn" disabled={busy || !text.trim()} onClick={send}>{busy ? 'Sending…' : 'Send'}</button>
      </div>
      {res && <p className="small">{res}</p>}
    </div>
  )
}
