import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Turns "repo/path/file.ext:12-34" citations into clickable links that open the code viewer.
const CITE = /((?:[\w.-]+\/)*[\w.-]+\.[A-Za-z]{1,6})\s*:\s*(\d+)(?:\s*-\s*(\d+))?/g

export default function Markdown({ text, sources = [], onOpen }) {
  const resolve = (full) => {
    for (const s of sources) {
      const p = `${s.repo}/${s.path}`
      if (full === p || full.endsWith(s.path) || p.endsWith(full)) return s
    }
    return null
  }
  const linkify = (str) => {
    const out = []
    let last = 0
    for (const m of str.matchAll(CITE)) {
      const src = resolve(m[1])
      if (!src) continue
      out.push(str.slice(last, m.index))
      out.push(
        <a key={m.index} className="cite" onClick={() => onOpen({ repo: src.repo, path: src.path, line: +m[2], end: m[3] ? +m[3] : +m[2] })}>
          {m[0]}
        </a>,
      )
      last = m.index + m[0].length
    }
    if (!out.length) return str
    out.push(str.slice(last))
    return out
  }
  const walk = (children) =>
    Array.isArray(children) ? children.map((c, i) => (typeof c === 'string' ? <span key={i}>{linkify(c)}</span> : c)) : typeof children === 'string' ? linkify(children) : children
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p>{walk(children)}</p>,
          li: ({ children }) => <li>{walk(children)}</li>,
          code: ({ children, className }) => (className ? <code className={className}>{children}</code> : <code>{walk(children)}</code>),
          a: ({ children }) => <span>{children}</span>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
