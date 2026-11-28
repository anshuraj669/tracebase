/*! Tracebase SDK: sends your app's errors and logs to your Tracebase. MIT. No dependencies, ~3 KB.
 *
 * Browser (one tag, before your app's scripts):
 *   <script src="https://YOUR-TRACEBASE/sdk/tracebase.js" data-key="tbk_..." data-release="1.4.2"></script>
 *
 * Node 18+ (copy this file into your project):
 *   const Tracebase = require('./tracebase.js')
 *   Tracebase.init({ url: 'https://YOUR-TRACEBASE', key: process.env.TRACEBASE_KEY, release: '1.4.2' })
 *
 * Then, anywhere:
 *   Tracebase.captureException(err, { orderId })     // errors you catch yourself
 *   Tracebase.log('warning', 'payment retried', { attempt: 2 })
 *   Tracebase.setUser('u_123')                       // an id, never an email or a name
 *
 * Uncaught errors and unhandled promise rejections are sent automatically, plus console.error (turn it off with
 * data-console="false" or { console: false }). Events are sent in small batches; Tracebase redacts personal data
 * before storing anything. The key is write-only: it can send this project's logs and nothing else.
 */
(function (root, factory) {
  var api = factory()
  if (typeof module === 'object' && module.exports) module.exports = api
  else root.Tracebase = api
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict'
  var cfg = null
  var queue = []
  var context = {}
  var timer = null
  var sending = false
  var MAX_QUEUE = 200
  var isBrowser = typeof window !== 'undefined' && typeof document !== 'undefined'

  function now() { return new Date().toISOString() }

  function stackLines(err) {
    var st = (err && err.stack) || ''
    return String(st).split('\n').map(function (l) { return l.trim() }).filter(function (l) { return l.indexOf('at ') === 0 || /@\S+:\d+/.test(l) })
  }

  function describe(err) {
    if (err instanceof Error) return [(err.name || 'Error') + ': ' + err.message, stackLines(err)]
    if (err && typeof err === 'object') {
      try { return [JSON.stringify(err).slice(0, 500), []] } catch (e) { return [String(err), []] }
    }
    return [String(err), []]
  }

  function fieldsLine(fields) {
    var all = {}
    var k
    for (k in context) all[k] = context[k]
    for (k in (fields || {})) all[k] = fields[k]
    if (cfg && cfg.release) all.release = cfg.release
    if (isBrowser && location) all.page = location.pathname
    var parts = []
    for (k in all) if (all[k] !== undefined && all[k] !== null && all[k] !== '') parts.push(k + '=' + String(all[k]).slice(0, 120))
    return parts.length ? ['context: ' + parts.join(', ')] : []
  }

  function push(level, message, stack, fields) {
    if (!cfg) return
    if (queue.length >= MAX_QUEUE) queue.shift() // keep the newest: a burst must not take the page down
    queue.push({ ts: now(), level: level, message: String(message).slice(0, 2000), stack: fieldsLine(fields).concat(stack || []) })
    if (level === 'error' || queue.length >= 20) schedule(200)
    else schedule(cfg.flushMs)
  }

  function schedule(ms) {
    if (timer) return
    timer = setTimeout(function () { timer = null; flush() }, ms)
  }

  function flush(final) {
    if (!cfg || !queue.length || (sending && !final)) return
    var batch = queue.splice(0, 50)
    sending = true
    var opts = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Tracebase-Key': cfg.key },
      body: JSON.stringify({ events: batch }),
      keepalive: !!final,
    }
    var done = function () { sending = false; if (queue.length) schedule(cfg.flushMs) }
    try {
      fetch(cfg.url + '/api/logs/ingest', opts).then(done, done)
    } catch (e) { done() }
  }

  function install() {
    if (isBrowser) {
      window.addEventListener('error', function (ev) {
        var d = describe(ev.error || ev.message)
        push('error', d[0], d[1].length ? d[1] : [ 'at ' + (ev.filename || '?') + ':' + ev.lineno + ':' + ev.colno ])
      })
      window.addEventListener('unhandledrejection', function (ev) { var d = describe(ev.reason); push('error', 'Unhandled rejection: ' + d[0], d[1]) })
      window.addEventListener('pagehide', function () { flush(true) })
      document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(true) })
    } else if (typeof process !== 'undefined' && process.on) {
      process.on('uncaughtException', function (err) {
        var d = describe(err)
        push('critical', d[0], d[1])
        flush(true)
        // keep Node's default behaviour (crash) once the event is on its way
        setTimeout(function () { process.exit(1) }, 1500)
      })
      process.on('unhandledRejection', function (reason) { var d = describe(reason); push('error', 'Unhandled rejection: ' + d[0], d[1]) })
      process.on('beforeExit', function () { flush(true) })
    }
    if (cfg.console && typeof console !== 'undefined') {
      ['error', 'warn'].forEach(function (lvl) {
        var orig = console[lvl]
        console[lvl] = function () {
          orig.apply(console, arguments)
          var args = Array.prototype.slice.call(arguments)
          var err = args.filter(function (a) { return a instanceof Error })[0]
          var text = args.filter(function (a) { return !(a instanceof Error) }).map(function (a) { return typeof a === 'string' ? a : '' }).join(' ').trim()
          var d = err ? describe(err) : [text, []]
          if (d[0]) push(lvl === 'warn' ? 'warning' : 'error', text && err ? text + ' ' + d[0] : d[0], d[1])
        }
      })
    }
  }

  var api = {
    init: function (options) {
      if (cfg) return api
      options = options || {}
      if (!options.key || String(options.key).indexOf('tbk_') !== 0) {
        if (typeof console !== 'undefined') console.warn('[Tracebase] init: a project key (tbk_...) is required')
        return api
      }
      cfg = {
        url: String(options.url || '').replace(/\/+$/, ''),
        key: options.key,
        release: options.release || '',
        console: options.console !== false,
        flushMs: options.flushMs || 3000,
      }
      install()
      return api
    },
    captureException: function (err, fields) { var d = describe(err); push('error', d[0], d[1], fields) },
    log: function (level, message, fields) { push(level || 'info', message, [], fields) },
    setUser: function (id) { context.user = id ? String(id).slice(0, 64) : undefined },
    setContext: function (fields) { for (var k in fields) context[k] = fields[k] },
    flush: function () { flush(true) },
  }

  // <script src=".../sdk/tracebase.js" data-key="tbk_..."> starts itself, sending to the server it was loaded from
  if (isBrowser && document.currentScript && document.currentScript.getAttribute('data-key')) {
    var s = document.currentScript
    api.init({
      url: s.getAttribute('data-url') || s.src.replace(/\/sdk\/tracebase\.js.*$/, ''),
      key: s.getAttribute('data-key'),
      release: s.getAttribute('data-release') || '',
      console: s.getAttribute('data-console') !== 'false',
    })
  }
  return api
})
