import { useState } from 'react'
import { api } from '../api.js'
import { Button, Card, Explain, Stat, TraceLink, usePolling, useAsyncAction } from './ui.jsx'

export default function PipelineCard() {
  const [status, setStatus] = useState(null)
  const [burst, setBurst] = useState(null)
  const [busy, run] = useAsyncAction()

  usePolling(async () => {
    const result = await api('/api/system/pipeline', { silent: true })
    if (result.ok) setStatus(result.data)
  }, 2000)

  const fire = (count) =>
    run(async () => setBurst(await api('/api/lab/events/burst', { method: 'POST', body: { count } })))

  const group = status?.groups?.[0]
  const publisher = status?.publisher

  return (
    <Card title="Asenkron audit/log pipeline'ı — Mongo'ya inline yazım yok" tag="Redis Streams" wide>
      <Explain
        what="İstek akışı olayı yalnız bellek kuyruğuna koyar (bloklamaz). Arka plan görevi toplu XADD ile Redis Stream'e yazar; worker'lar consumer group ile okuyup MongoDB'ye toplu upsert eder, sonra ACK'ler. Worker/Mongo yavaşsa istek gecikmesi etkilenmez; olaylar stream'de birikir."
        where="`make worker-stop` → lag/pending artar · `make worker-start` → boşalır · Grafana → 'Asenkron event/log pipeline'ı'."
      />
      <div className="row">
        <Button busy={busy} onClick={() => fire(1000)}>
          1.000 olay üret
        </Button>
        <Button busy={busy} variant="ghost" onClick={() => fire(10000)}>
          10.000 olay üret
        </Button>
        {burst?.ok && (
          <span className="muted">
            kabul: {burst.data.accepted} · düşürülen: {burst.data.dropped}
          </span>
        )}
      </div>
      {status && (
        <div className="stats">
          <Stat
            label={`API bellek kuyruğu (${status.served_by})`}
            value={`${publisher.queue_size} / ${publisher.queue_capacity}`}
          />
          <Stat label="Stream uzunluğu" value={status.stream?.length ?? '—'} />
          <Stat label="Consumer lag" value={group?.lag ?? '—'} tone={group?.lag > 1000 ? 'warn' : undefined} />
          <Stat label="Pending (ACK bekleyen)" value={group?.pending ?? '—'} />
          <Stat label="Consumer sayısı" value={group?.consumers ?? '—'} />
          <Stat label="DLQ" value={status.dlq_length ?? '—'} tone={status.dlq_length ? 'bad' : 'good'} />
          <Stat label="Mongo'daki olay" value={status.persisted_total?.toLocaleString('tr-TR') ?? '—'} />
        </div>
      )}
      {status?.recent_events?.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Olay</th>
              <th>Kaynak</th>
              <th>Zaman</th>
              <th>Pipeline gecikmesi</th>
              <th>Trace</th>
            </tr>
          </thead>
          <tbody>
            {status.recent_events.map((e) => (
              <tr key={e.event_id}>
                <td>{e.type}</td>
                <td>{e.source}</td>
                <td>{new Date(e.ts).toLocaleTimeString()}</td>
                <td>{e.pipeline_latency_ms != null ? `${e.pipeline_latency_ms} ms` : '—'}</td>
                <td>
                  <TraceLink traceId={e.trace_id} label="" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
