import { useEffect, useState } from 'react'
import { api, config, logsUrl, onTrace } from './api.js'
import ProductsCard from './components/ProductsCard.jsx'
import { FlashSaleLab, IdempotencyLab, LockLab, RateLimitLab, StampedeLab } from './components/ConcurrencyLabs.jsx'
import PipelineCard from './components/PipelineCard.jsx'
import RedisHaCard from './components/RedisHaCard.jsx'
import { TraceLink } from './components/ui.jsx'

export default function App() {
  const [info, setInfo] = useState(null)
  const [lastCall, setLastCall] = useState(null)

  useEffect(() => {
    api('/api/system/info', { silent: true }).then((result) => result.ok && setInfo(result.data))
    return onTrace(setLastCall)
  }, [])

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>VEON Best-Practice Lab</h1>
          <p className="muted">
            Tracing · Monitoring · Logging · Redis HA &amp; eşzamanlılık · Asenkron log/event pipeline'ı
          </p>
        </div>
        <nav className="links">
          <a href={`${config.grafanaUrl}/d/veon-sample-overview`} target="_blank" rel="noreferrer">
            Grafana dashboard ↗
          </a>
          <a href={`${config.grafanaUrl}/explore`} target="_blank" rel="noreferrer">
            Explore (Tempo/Loki) ↗
          </a>
          {config.jaegerUrl && (
            <a href={`${config.jaegerUrl}/search?service=veon-sample-backend`} target="_blank" rel="noreferrer">
              Jaeger ↗
            </a>
          )}
          <a href={`${config.prometheusUrl}/alerts`} target="_blank" rel="noreferrer">
            Prometheus alarmları ↗
          </a>
          <a href={`${config.kibanaUrl}/app/discover`} target="_blank" rel="noreferrer">
            Kibana (EFK profili) ↗
          </a>
        </nav>
      </header>

      <div className="infobar">
        {info ? (
          <>
            <span>
              <strong>{info.service}</strong> v{info.version} · {info.environment}
            </span>
            <span>redis: {info.redis_mode}</span>
            <span>tracing: {info.tracing ? 'açık' : 'kapalı'}</span>
            <span>yanıt veren: {info.served_by}</span>
          </>
        ) : (
          <span>API'ye ulaşılamıyor…</span>
        )}
        {lastCall && (
          <span className="last-call">
            son istek: {lastCall.status} · {lastCall.ms} ms · <TraceLink traceId={lastCall.traceId} />{' '}
            <a href={logsUrl(lastCall.traceId)} target="_blank" rel="noreferrer">
              loglar ↗
            </a>
          </span>
        )}
      </div>

      <main className="grid">
        <RedisHaCard />
        <ProductsCard />
        <StampedeLab />
        <FlashSaleLab />
        <IdempotencyLab />
        <RateLimitLab />
        <LockLab />
        <PipelineCard />
      </main>

      <footer className="footer muted">
        Lab uç noktaları yalnız LAB_ENDPOINTS_ENABLED=true ortamlarda açıktır. Ayrıntılar: README.md ve docs/.
      </footer>
    </div>
  )
}
