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
  // Sign-in
  authConfig: () => req('GET', '/api/auth/config', undefined, ''),
  googleSignIn: (credential) => req('POST', '/api/auth/google', { credential }, ''),
  signOut: () => req('DELETE', '/api/auth/session').catch(() => {}),
  // Team (admin)
  members: () => req('GET', '/api/team/users'),
  addMember: (m) => req('POST', '/api/team/users', m),
  setMemberEmail: (name, email) => req('PUT', `/api/team/users/${encodeURIComponent(name)}/email`, { email: email || null }),
  setMemberRepos: (name, repos) => req('PUT', `/api/team/users/${encodeURIComponent(name)}/repos`, repos),
  removeMember: (name) => req('DELETE', `/api/team/users/${encodeURIComponent(name)}`),
  // Connected projects
  projects: () => req('GET', '/api/projects'),
  project: (name) => req('GET', `/api/projects/${encodeURIComponent(name)}`),
  addProject: (name, platform) => req('POST', '/api/projects', { name, platform }),
  rotateProject: (name) => req('POST', `/api/projects/${encodeURIComponent(name)}/rotate`),
  removeProject: (name, deleteData) => req('DELETE', `/api/projects/${encodeURIComponent(name)}?${qs({ delete_data: deleteData ? 'true' : undefined })}`),
  testProject: (name) => req('POST', `/api/projects/${encodeURIComponent(name)}/test`),
  // Log Detective
  errors: ({ since, service, q }) => req('GET', `/api/logs/groups?${qs({ since, service, q, limit: 100 })}`),
  logGroup: (code) => req('GET', `/api/logs/groups/${code}`),
  explainError: (code, refresh) => req('POST', `/api/logs/groups/${code}/explain${refresh ? '?refresh=true' : ''}`),
  setErrorStatus: (code, status) => req('POST', `/api/logs/groups/${code}/status`, { status }),
  logServices: () => req('GET', '/api/logs/services'),
  logTimeline: (service) => req('GET', `/api/logs/timeline?${qs({ service, hours: 48 })}`),
  // Feature flags (Flagsmith)
  errorFlags: (code) => req('GET', `/api/logs/groups/${code}/flags`),
  rollbackFlag: (code, changeId) => req('POST', `/api/logs/groups/${code}/rollback`, { change_id: changeId, confirm: true }),
  flagChanges: (since) => req('GET', `/api/flags/changes?${qs({ since, limit: 200 })}`),
  alerts: (unread) => req('GET', `/api/alerts${unread ? '?unread=true' : ''}`),
  readAlerts: () => req('POST', '/api/alerts/read', {}),
  ingest: (text, service) => req('POST', `/api/logs/ingest?${qs({ service })}`, text, auth.get(), true),
  // Data & privacy (admin)
  dataSummary: () => req('GET', '/api/data/summary'),
  dataPrune: () => req('POST', '/api/data/prune'),
  forgetRepo: (name) => req('DELETE', `/api/data/repos/${name.split('/').map(encodeURIComponent).join('/')}`),
  restoreRepo: (name) => req('POST', `/api/data/repos/${name.split('/').map(encodeURIComponent).join('/')}/restore`),
  forgetService: (name, exclude) => req('DELETE', `/api/data/services/${encodeURIComponent(name)}?${qs({ exclude: exclude || undefined })}`),
  restoreService: (name) => req('POST', `/api/data/services/${encodeURIComponent(name)}/restore`),
  purge: (match, dryRun) => req('POST', '/api/data/purge', { match, dry_run: !!dryRun }),
  // NDJSON download (with the token in a header, so not a plain link)
  exportData: async (service, match) => {
    const token = auth.get()
    const res = await fetch(`/api/data/export?${qs({ service, match })}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
    const url = URL.createObjectURL(await res.blob())
    const a = Object.assign(document.createElement('a'), { href: url, download: `tracebase-export-${service || 'all'}.ndjson` })
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  },
}
