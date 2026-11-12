import { useEffect, useRef, useState } from 'react'
import { jaegerTraceUrl, traceUrl } from '../api.js'

export function Card({ title, tag, wide, children }) {
  return (
    <section className={`card${wide ? ' wide' : ''}`}>
      <header className="card-head">
        <h2>{title}</h2>
        {tag && <span className="tag">{tag}</span>}
      </header>
      {children}
    </section>
  )
}

export function Explain({ what, where }) {
  return (
    <div className="explain">
      <p>{what}</p>
      {where && (
        <p className="where">
          <strong>Nereye bakılır:</strong> {where}
        </p>
      )}
    </div>
  )
}

export function Button({ busy, children, variant = 'primary', ...props }) {
  return (
    <button className={`btn ${variant}`} disabled={busy || props.disabled} {...props}>
      {busy ? 'Çalışıyor…' : children}
    </button>
  )
}

export function Field({ label, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  )
}

export function TraceLink({ traceId, label = 'trace' }) {
  if (!traceId) return null
  const jaeger = jaegerTraceUrl(traceId)
  return (
    <span className="trace-links">
      <a className="trace-link" href={traceUrl(traceId)} target="_blank" rel="noreferrer" title={`Tempo (Grafana): ${traceId}`}>
        {label} {traceId.slice(0, 8)}… ↗
      </a>
      {jaeger && (
        <a className="trace-link alt" href={jaeger} target="_blank" rel="noreferrer" title={`Jaeger: ${traceId}`}>
          Jaeger ↗
        </a>
      )}
    </span>
  )
}

export function Stat({ label, value, tone }) {
  return (
    <div className={`stat${tone ? ` ${tone}` : ''}`}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}

export function Tally({ title, data, labels = {}, tones = {} }) {
  const entries = Object.entries(data || {}).filter(([key]) => key !== 'None' && key !== 'null')
  if (!entries.length) return null
  const total = entries.reduce((sum, [, n]) => sum + n, 0)
  return (
    <div className="tally">
      <div className="tally-title">{title}</div>
      {entries
        .sort((a, b) => b[1] - a[1])
        .map(([key, n]) => (
          <div className="tally-row" key={key}>
            <span className="tally-key">{labels[key] || key}</span>
            <span className="tally-bar">
              <span className={`tally-fill ${tones[key] || ''}`} style={{ width: `${(n / total) * 100}%` }} />
            </span>
            <span className="tally-n">{n}</span>
          </div>
        ))}
    </div>
  )
}

export function FanoutSummary({ result }) {
  if (!result) return null
  if (!result.ok) return <ErrorBox result={result} />
  const { summary, elapsed_ms: elapsed, trace_id: traceId, count } = result.data
  return (
    <div className="fanout">
      <div className="fanout-meta">
        {count} eşzamanlı istek · {Math.round(elapsed)} ms <TraceLink traceId={traceId} label="tüm fırtına" />
      </div>
      <div className="tallies">
        <Tally title="HTTP status" data={summary.status} />
        <Tally title="Hangi replika (X-Served-By)" data={summary.served_by} />
      </div>
    </div>
  )
}

export function ErrorBox({ result }) {
  if (!result || result.ok) return null
  return (
    <div className="error-box">
      HTTP {result.status || 'ağ hatası'} — <code>{JSON.stringify(result.data)}</code>
    </div>
  )
}

export function usePolling(fn, intervalMs, enabled = true) {
  const saved = useRef(fn)
  saved.current = fn
  useEffect(() => {
    if (!enabled) return undefined
    let stopped = false
    let timer
    const tick = async () => {
      await saved.current()
      if (!stopped) timer = setTimeout(tick, intervalMs)
    }
    tick()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [intervalMs, enabled])
}

export function useAsyncAction() {
  const [busy, setBusy] = useState(false)
  const run = async (fn) => {
    setBusy(true)
    try {
      return await fn()
    } finally {
      setBusy(false)
    }
  }
  return [busy, run]
}
