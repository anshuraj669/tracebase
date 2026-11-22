// Team mode: the member's token, kept in this browser only (never put in URLs)
const TOKEN_KEY = 'tracebase_token'
export const auth = {
  get: () => { try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' } },
  set: (t) => { try { localStorage.setItem(TOKEN_KEY, t) } catch { /* private window */ } },
  clear: () => { try { localStorage.removeItem(TOKEN_KEY) } catch { /* ignore */ } },
}

async function req(method, url, body, token = auth.get(), raw = false) {
  const headers = {}
  if (body) headers['Content-Type'] = raw ? 'text/plain' : 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(url, { method, headers, body: body ? (raw ? body : JSON.stringify(body)) : undefined })
  const data = await res.json().catch(() => ({}))
  if (res.status === 401) window.dispatchEvent(new Event('tracebase:signin'))
  if (!res.ok) throw new Error(data.detail || res.statusText)
  return data
}

const qs = (o) =>
  new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '')).toString()

export const api = {
  health: () => req('GET', '/api/health'),
  stats: () => req('GET', '/api/stats'),
  repos: () => req('GET', '/api/code/repos'),
  tree: (repo) => req('GET', `/api/code/tree?${qs({ repo })}`),
  search: (q, repo) => req('GET', `/api/code/search?${qs({ q, repo, limit: 15 })}`),
  grep: (pattern, repo, regex) => req('GET', `/api/code/grep?${qs({ pattern, repo, regex })}`),
  ask: (question, repo) => req('POST', '/api/code/ask', { question, repo: repo || null }),
  file: (repo, path) => req('GET', `/api/code/file?${qs({ repo, path })}`),
  explain: (repo, path, line) => req('POST', '/api/code/explain', { repo, path, line }),
  history: (repo, path, line) => req('GET', `/api/code/history?${qs({ repo, path, line })}`),
  secrets: () => req('GET', '/api/code/secrets'),
  reindex: () => req('POST', '/api/code/reindex'),
  me: (token) => req('GET', '/api/team/me', undefined, token),
  // Log Detective
  errors: ({ since, service, q }) => req('GET', `/api/logs/groups?${qs({ since, service, q, limit: 100 })}`),
  logGroup: (code) => req('GET', `/api/logs/groups/${code}`),
  explainError: (code, refresh) => req('POST', `/api/logs/groups/${code}/explain${refresh ? '?refresh=true' : ''}`),
  setErrorStatus: (code, status) => req('POST', `/api/logs/groups/${code}/status`, { status }),
  logServices: () => req('GET', '/api/logs/services'),
  logTimeline: (service) => req('GET', `/api/logs/timeline?${qs({ service, hours: 48 })}`),
  alerts: (unread) => req('GET', `/api/alerts${unread ? '?unread=true' : ''}`),
  readAlerts: () => req('POST', '/api/alerts/read', {}),
  ingest: (text, service) => req('POST', `/api/logs/ingest?${qs({ service })}`, text, auth.get(), true),
}
