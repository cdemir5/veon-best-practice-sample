import { useState } from 'react'
import { api } from '../api.js'
import { Button, Card, Explain, Stat, usePolling, useAsyncAction } from './ui.jsx'

const WINDOW = 90

export default function RedisHaCard() {
  const [beats, setBeats] = useState([])
  const [topology, setTopology] = useState(null)
  const [failover, setFailover] = useState(null)
  const [busy, run] = useAsyncAction()

  usePolling(async () => {
    const result = await api('/api/system/heartbeat', { silent: true })
    setBeats((prev) => [...prev.slice(-(WINDOW - 1)), { ...result.data, ok: result.ok, at: Date.now() }])
  }, 1000)

  usePolling(async () => {
    const result = await api('/api/system/redis', { silent: true })
    if (result.ok) setTopology(result.data)
  }, 3000)

  const trigger = () => run(async () => setFailover(await api('/api/lab/redis/failover', { method: 'POST' })))

  const last = beats[beats.length - 1]
  const failures = beats.filter((b) => !b.ok).length
  const primary = topology?.primary

  return (
    <Card title="Redis HA — Sentinel failover" tag="1 master · 2 replica · 3 sentinel" wide>
      <Explain
        what="Uygulama Redis'e doğrudan değil Sentinel üzerinden bağlanır (master_for). Master düşünce sentinel'ler (quorum=2) bir replikayı terfi ettirir; redis-py yeni master'ı Sentinel'den öğrenip jitter'lı backoff ile yeniden bağlanır. Aşağıdaki şerit saniyede bir INCR atar: failover penceresi kırmızı görünür."
        where="Sert kesinti: `make failover-hard` (master container'ını durdurur) · Grafana → 'Redis rolleri (failover zaman çizelgesi)'."
      />
      <div className="heartbeat">
        {beats.map((b) => (
          <span
            key={b.at}
            className={`beat ${b.ok ? 'ok' : 'fail'}`}
            title={`${new Date(b.at).toLocaleTimeString()} · ${b.ok ? `${b.latency_ms} ms · ${b.master}` : b.error_type}`}
            style={{ height: `${b.ok ? Math.min(100, 20 + (b.latency_ms || 0) * 4) : 100}%` }}
          />
        ))}
      </div>
      <div className="stats">
        <Stat label="Şu anki master" value={last?.master || topology?.master || '—'} />
        <Stat label="Son gecikme" value={last?.ok ? `${last.latency_ms} ms` : last?.error_type || '—'} tone={last && !last.ok ? 'bad' : undefined} />
        <Stat label={`Başarısız (${WINDOW} sn)`} value={failures} tone={failures ? 'warn' : 'good'} />
        <Stat label="Bağlı replika" value={primary?.connected_replicas ?? '—'} />
        <Stat label="maxmemory-policy" value={primary?.maxmemory_policy ?? '—'} />
        <Stat label="ops/sn" value={primary?.ops_per_sec ?? '—'} />
      </div>
      <div className="row">
        <Button busy={busy} variant="warn" onClick={trigger}>
          Kontrollü failover (SENTINEL FAILOVER)
        </Button>
        {failover && (
          <span className="muted">
            {failover.ok ? `İstendi: ${failover.data.via} — ~5-10 sn içinde yeni master` : JSON.stringify(failover.data)}
          </span>
        )}
      </div>
      {topology?.sentinels && (
        <table className="table">
          <thead>
            <tr>
              <th>Sentinel</th>
              <th>Gördüğü master</th>
              <th>Durum</th>
              <th>Diğer sentinel</th>
              <th>Replika</th>
              <th>Quorum</th>
            </tr>
          </thead>
          <tbody>
            {topology.sentinels.map((s) => (
              <tr key={s.address}>
                <td>{s.address}</td>
                <td>{s.sees_master || '—'}</td>
                <td className={s.ok ? '' : 'bad'}>{s.ok ? s.flags : s.error_type}</td>
                <td>{s.other_sentinels ?? '—'}</td>
                <td>{s.replicas ?? '—'}</td>
                <td>{s.quorum ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {primary?.replicas?.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Replika</th>
              <th>Durum</th>
              <th>Offset farkı (byte)</th>
              <th>Lag (sn)</th>
            </tr>
          </thead>
          <tbody>
            {primary.replicas.map((r) => (
              <tr key={r.address}>
                <td>{r.address}</td>
                <td>{r.state}</td>
                <td>{r.offset_lag}</td>
                <td>{r.lag_seconds}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
