import { useEffect, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'

const PLATFORMS = [
  { id: 'browser', label: 'Browser app', hint: 'React, Vue, plain JS: one script tag' },
  { id: 'node', label: 'Node.js', hint: 'Express, Next.js server, workers' },
  { id: 'python', label: 'Python', hint: 'Django, FastAPI, Flask, scripts' },
  { id: 'logs', label: 'Log files', hint: 'Laravel, Nginx, any log file' },
  { id: 'other', label: 'Any language', hint: 'one HTTP call' },
]

const ago = (ts) => {
  if (!ts) return null
  const s = (Date.now() - new Date(ts.replace(' ', 'T') + (ts.endsWith('Z') ? '' : 'Z')).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

export default function ProjectsPage({ selected, me }) {
  const isAdmin = !me || me.role === 'admin'
  const [items, setItems] = useState(null)
  const [err, setErr] = useState(null)
  const [fresh, setFresh] = useState({}) // name -> key, only right after create/rotate (never stored)
  const load = () => api.projects().then(setItems).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [selected])

  if (selected) {
    return <ProjectDetail name={decodeURIComponent(selected)} isAdmin={isAdmin} freshKey={fresh[decodeURIComponent(selected)]}
      onKey={(name, key) => setFresh({ ...fresh, [name]: key })} onGone={() => { location.hash = 'projects'; load() }} />
  }
  return (
    <div>
      <p className="muted lead">Connect your apps so their errors and logs come here on their own: Log Detective groups them, explains them with your code and Git history, and alerts you. Each project gets a write-only key that can send its own logs and nothing else.</p>
      {isAdmin && <NewProject onCreated={(p) => { setFresh({ ...fresh, [p.name]: p.key }); location.hash = `projects/${encodeURIComponent(p.name)}` }} />}
      {err && <div className="error">{err}</div>}
      <div className="card list proj-list">
        <div className="phead pad"><h3>Connected projects</h3><span className="muted small">{items ? items.length : '…'}</span></div>
        {items?.map((p) => (
          <a key={p.name} className="row proj-row" href={`#projects/${encodeURIComponent(p.name)}`}>
            <span className={`dotlive ${p.last_event_at ? 'on' : ''}`} title={p.last_event_at ? 'receiving events' : 'no events yet'} />
            <div className="err-main">
              <b>{p.name}</b>
              <span className="muted small">{PLATFORMS.find((x) => x.id === p.platform)?.label || p.platform} · {p.last_event_at ? `last event ${ago(p.last_event_at)}` : 'waiting for its first event'}</span>
            </div>
            {p.open_errors > 0 && <span className="badge st-new">{p.open_errors} open error{p.open_errors === 1 ? '' : 's'}</span>}
            <span className="err-n">{p.events.toLocaleString()} <span className="muted small">events</span></span>
          </a>
        ))}
        {items && !items.length && <p className="muted pad">No connected projects yet.{isAdmin ? ' Create one above: it takes a minute.' : ' Ask an admin to connect one.'}</p>}
      </div>
    </div>
  )
}

function NewProject({ onCreated }) {
  const [name, setName] = useState('')
  const [platform, setPlatform] = useState('browser')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const slug = name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+/, '').slice(0, 63)
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      onCreated(await api.addProject(slug, platform))
    } catch (e2) {
      setErr(e2.message)
      setBusy(false)
    }
  }
  return (
    <form className="card panel new-proj" onSubmit={submit}>
      <div className="phead"><h3>Connect a project</h3></div>
      <label className="fld">
        <span>Project name <span className="muted small">(its logs and errors are filed under this name)</span></span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="meetsnap-web" aria-label="Project name" />
        {name && slug !== name && <span className="muted small">Will be saved as <code>{slug}</code></span>}
      </label>
      <div className="fld">
        <span>What is it?</span>
        <div className="plat-grid" role="radiogroup" aria-label="Platform">
          {PLATFORMS.map((p) => (
            <button type="button" key={p.id} role="radio" aria-checked={platform === p.id}
              className={`plat ${platform === p.id ? 'on' : ''}`} onClick={() => setPlatform(p.id)}>
              <b>{p.label}</b><span>{p.hint}</span>
            </button>
          ))}
        </div>
      </div>
      {err && <div className="error">{err}</div>}
      <div><button className="primary btn" disabled={busy || slug.length < 2}>{busy ? 'Creating…' : 'Create project and get a key'}</button></div>
    </form>
  )
}

function ProjectDetail({ name, isAdmin, freshKey, onKey, onGone }) {
  const [p, setP] = useState(null)
  const [err, setErr] = useState(null)
  const [tab, setTab] = useState(null)
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true
    const get = () => api.project(name).then((x) => { if (live) { setP(x); setTab((t) => t || x.platform) } }).catch((e) => live && setErr(e.message))
    get()
    const t = setInterval(get, 4000) // "waiting for the first event" turns green on its own
    return () => { live = false; clearInterval(t) }
  }, [name])

  if (err) return <div><a className="link-btn" href="#projects">← All projects</a><div className="error">{err}</div></div>
  if (!p) return <p className="muted">Loading…</p>

  const origin = location.origin
  const key = freshKey || 'YOUR_PROJECT_KEY'
  const act = async (fn, done) => {
    setBusy(true)
    setMsg(null)
    try { done(await fn()) } catch (e) { setMsg({ bad: true, text: e.message }) } finally { setBusy(false) }
  }
  const rotate = () => {
    if (!confirm(`Make a new key for ${name}? The current key stops working at once: update the app's configuration.`)) return
    act(() => api.rotateProject(name), (r) => { onKey(name, r.key); setMsg({ text: 'New key created. Copy it now: it is shown only once.' }) })
  }
  const remove = () => {
    if (!confirm(`Disconnect ${name}? Its key stops working.`)) return
    const del = confirm(`Also delete all of ${name}'s logs and errors stored in Tracebase?\n\nOK = delete them, Cancel = keep them.`)
    act(() => api.removeProject(name, del), onGone)
  }
  const test = () => act(() => api.testProject(name), (r) => setMsg({ text: `Test event received${r.new_groups?.length ? `: ${r.new_groups.join(', ')} in Log Detective` : ''}.` }))

  return (
    <div>
      <a className="link-btn" href="#projects">← All projects</a>
      <div className="err-head">
        <h2>{p.name}</h2>
        <div className="muted">{PLATFORMS.find((x) => x.id === p.platform)?.label || p.platform} · created by {p.created_by || '?'} {p.created_at?.slice(0, 10)}</div>
      </div>

      <div className={`card panel conn ${p.last_event_at ? 'ok' : ''}`}>
        {p.last_event_at
          ? <><Icon name="check" /> <div><b>Connected.</b> {p.events.toLocaleString()} events so far, the last {ago(p.last_event_at)}.{p.open_errors ? <> {p.open_errors} open error type{p.open_errors === 1 ? '' : 's'}: <a href="#logs">open Log Detective</a>.</> : ''}</div></>
          : <><span className="spin" /> <div><b>Waiting for the first event…</b> Add the snippet below to your app and load a page or trigger an error. This updates on its own.</div></>}
      </div>

      <div className="card panel">
        <div className="phead"><h3>Project key</h3>{isAdmin && <button onClick={rotate} disabled={busy}>Make a new key</button>}</div>
        {freshKey ? (
          <>
            <CopyLine text={freshKey} />
            <p className="small warnline">Copy it now: it is shown only once. It is write-only (it can send this project's logs and nothing else), so it is fine in a browser bundle or an environment variable.</p>
          </>
        ) : (
          <p className="muted small">Starts with <code>{p.key_hint}…</code>. For safety the full key is shown only when it is created.{isAdmin ? ' Lost it? Make a new key.' : ''}</p>
        )}
      </div>

      <div className="card panel">
        <div className="phead"><h3>Set it up</h3></div>
        <div className="seg" role="tablist">
          {PLATFORMS.map((x) => (
            <button key={x.id} role="tab" aria-selected={tab === x.id} className={tab === x.id ? 'on' : ''} onClick={() => setTab(x.id)}>{x.label}</button>
          ))}
        </div>
        <Snippets platform={tab} origin={origin} keyText={key} />
      </div>

      {msg && <div className={msg.bad ? 'error' : 'okmsg'}>{msg.text}</div>}
      {isAdmin && (
        <div className="row-between proj-actions">
          <button onClick={test} disabled={busy}>Send a test event</button>
          <button className="danger-link" onClick={remove} disabled={busy}>Disconnect project</button>
        </div>
      )}
    </div>
  )
}

function Snippets({ platform, origin, keyText }) {
  const S = {
    browser: [
      ['Add this to your page, before your own scripts (index.html):', `<script src="${origin}/sdk/tracebase.js"\n        data-key="${keyText}" data-release="1.0.0"></script>`],
      ['Uncaught errors, failed promises and console.error are sent automatically. For errors you catch yourself:', `try {\n  await saveOrder(cart)\n} catch (err) {\n  Tracebase.captureException(err, { orderId: cart.id })\n  throw err\n}\n\nTracebase.setUser(user.id)   // an id: never an email or a name`],
      ['Building with Vite or webpack? Point Tracebase at the code: put the repo in the repos folder, and errors show the failing line and the commit that changed it.', null],
    ],
    node: [
      ['Download the SDK into your project (no dependencies, Node 18+):', `curl -o tracebase.js ${origin}/sdk/tracebase.js`],
      ['Start it first thing in your app:', `const Tracebase = require('./tracebase.js')\n\nTracebase.init({\n  url: '${origin}',\n  key: process.env.TRACEBASE_KEY,   // ${keyText}\n  release: process.env.APP_VERSION,\n})`],
      ['Crashes and unhandled rejections are sent automatically. In an Express error handler:', `app.use((err, req, res, next) => {\n  Tracebase.captureException(err, { route: req.path })\n  next(err)\n})`],
    ],
    python: [
      ['Install the Tracebase package (standard library only):', 'pip install "tracebase-cli>=0.4"'],
      ['Add the handler where you configure logging:', `import logging, os\nfrom tracebase_cli.handler import TracebaseHandler, capture_uncaught\n\nhandler = TracebaseHandler(\n    url="${origin}",\n    key=os.environ["TRACEBASE_KEY"],   # ${keyText}\n)\nlogging.getLogger().addHandler(handler)   # WARNING and above\ncapture_uncaught(handler)                 # crashes too`],
      ['logger.exception(...) sends the full traceback; extra={...} fields are kept as context.', `log = logging.getLogger("payments")\ntry:\n    charge(order)\nexcept Exception:\n    log.exception("charge failed", extra={"order_id": order.id})`],
    ],
    logs: [
      ['Send a log file (Laravel, Nginx, Django, JSON lines…) with the Tracebase CLI:', `pip install tracebase-cli\nTRACEBASE_URL=${origin} TRACEBASE_TOKEN=${keyText} tracebase ingest storage/logs/laravel.log`],
      ['Or with curl, from a cron job or a deploy script:', `curl -X POST ${origin}/api/logs/ingest \\\n  -H "X-Tracebase-Key: ${keyText}" \\\n  -H "Content-Type: text/plain" \\\n  --data-binary @/var/log/nginx/error.log`],
      ['On the Tracebase server itself, log files dropped in the logs folder are read on their own.', null],
    ],
    other: [
      ['Any language: POST events as JSON. A stack trace lets Tracebase find the failing line in your code.', `curl -X POST ${origin}/api/logs/ingest \\\n  -H "X-Tracebase-Key: ${keyText}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"events": [{\n        "level": "error",\n        "message": "PaymentError: card declined",\n        "stack": ["at charge (src/payments.ts:42:7)"]\n      }]}'`],
      ['Fields: ts (ISO time, optional), level (debug, info, warning, error, critical), message, stack (lines). Up to 6000 events a minute per project.', null],
    ],
  }
  return (
    <div className="snips">
      {(S[platform] || []).map(([text, code], i) => (
        <div key={i}>
          <p className="small">{text}</p>
          {code && <CodeBlock code={code} />}
        </div>
      ))}
    </div>
  )
}

function CodeBlock({ code }) {
  const [done, setDone] = useState(false)
  const copy = () => { navigator.clipboard?.writeText(code); setDone(true); setTimeout(() => setDone(false), 1500) }
  return (
    <div className="codeblock">
      <pre>{code}</pre>
      <button className="ghost copybtn" onClick={copy} title="Copy" aria-label="Copy">{done ? <Icon name="check" size={16} /> : <Icon name="copy" size={16} />}</button>
    </div>
  )
}

function CopyLine({ text }) {
  const [done, setDone] = useState(false)
  return (
    <div className="copyline">
      <code>{text}</code>
      <button onClick={() => { navigator.clipboard?.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500) }}>{done ? 'Copied' : 'Copy'}</button>
    </div>
  )
}
