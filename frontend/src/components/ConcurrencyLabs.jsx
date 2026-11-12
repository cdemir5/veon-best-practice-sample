import { useState } from 'react'
import { api, CLIENT_ID, fanout, uid } from '../api.js'
import {
  Button,
  Card,
  ErrorBox,
  Explain,
  FanoutSummary,
  Field,
  Stat,
  Tally,
  useAsyncAction,
} from './ui.jsx'

const SKUS = ['SKU-1', 'SKU-2', 'SKU-3', 'SKU-4', 'SKU-5']
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// ------------------------------------------------------------------ cache stampede
export function StampedeLab() {
  const [sku, setSku] = useState('SKU-1')
  const [count, setCount] = useState(50)
  const [protect, setProtect] = useState(true)
  const [state, setState] = useState(null)
  const [busy, run] = useAsyncAction()

  const start = () =>
    run(async () => {
      await api(`/api/lab/cache/${sku}/flush`, { method: 'POST', silent: true })
      const result = await fanout({ path: `/api/products/${sku}?protect=${protect}`, count })
      const loads = await api(`/api/lab/cache/${sku}/db-loads`, { silent: true })
      setState({ result, loads: loads.data?.db_loads, count })
    })

  return (
    <Card title="Cache stampede (thundering herd)" tag="Redis lock">
      <Explain
        what="Cache boşken aynı anahtara N eşzamanlı istek gelir. Koruma açıkken yalnız biri (SET NX kilidi alan) Mongo'ya gider, diğerleri bekleyip cache'ten okur — iki replika arasında bile."
        where="Grafana → 'Cache sonuçları vs. Mongo okumaları' (coalesced ↑, mongo load = 1)."
      />
      <div className="row">
        <Field label="Ürün">
          <select value={sku} onChange={(e) => setSku(e.target.value)}>
            {SKUS.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </Field>
        <Field label="İstek">
          <input type="number" value={count} min="2" max="300" onChange={(e) => setCount(+e.target.value)} />
        </Field>
        <label className="toggle">
          <input type="checkbox" checked={protect} onChange={(e) => setProtect(e.target.checked)} />
          Stampede koruması
        </label>
        <Button busy={busy} onClick={start}>
          Soğuk cache'e saldır
        </Button>
      </div>
      {state && (
        <>
          <div className="stats">
            <Stat
              label="Mongo'ya giden istek"
              value={`${state.loads ?? '?'} / ${state.count}`}
              tone={state.loads > 1 ? 'bad' : 'good'}
            />
          </div>
          <FanoutSummary result={state.result} />
          {state.result.ok && <Tally title="X-Cache" data={state.result.data.summary.cache} />}
        </>
      )}
    </Card>
  )
}

// ------------------------------------------------------------------ flash sale
export function FlashSaleLab() {
  const [stock, setStock] = useState(10)
  const [count, setCount] = useState(100)
  const [mode, setMode] = useState('atomic')
  const [state, setState] = useState(null)
  const [busy, run] = useAsyncAction()
  const sku = 'SKU-FLASH'

  const start = () =>
    run(async () => {
      await api(`/api/lab/flash-sale/${sku}/reset`, { method: 'POST', body: { stock }, silent: true })
      const result = await fanout({
        method: 'POST',
        path: `/api/flash-sale/${sku}/reserve?mode=${mode}`,
        count,
        body: { user_id: 'user-{i}', quantity: 1 },
      })
      const status = await api(`/api/flash-sale/${sku}`, { silent: true })
      setState({ result, status: status.data, stock, mode })
    })

  const sold = state?.result.ok ? state.result.data.summary.status['201'] || 0 : 0
  const oversold = state ? sold - state.stock : 0

  return (
    <Card title="Flash-sale — atomik Lua vs. naif kod" tag="Redis Lua">
      <Explain
        what="N kullanıcı aynı anda stok düşürmeye çalışır. Naif kod GET→kontrol→SET yapar ve araya giren istekler aynı stoğu okur (fazla satış). Lua script kontrol+düşüm+alıcı kaydını tek atomik adımda yapar."
        where="Grafana → 'Eşzamanlılık primitifleri' (flash atomic/naive); başarılı rezervasyonlar 'Asenkron pipeline' ile Mongo'ya akar."
      />
      <div className="row">
        <Field label="Stok">
          <input type="number" value={stock} min="1" max="1000" onChange={(e) => setStock(+e.target.value)} />
        </Field>
        <Field label="Alıcı">
          <input type="number" value={count} min="2" max="300" onChange={(e) => setCount(+e.target.value)} />
        </Field>
        <Field label="Mod">
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="atomic">atomic (Lua)</option>
            <option value="naive">naive (GET→SET)</option>
          </select>
        </Field>
        <Button busy={busy} onClick={start}>
          Kampanyayı başlat
        </Button>
      </div>
      {state && (
        <>
          <div className="stats">
            <Stat label="Satılan" value={sold} />
            <Stat label="Stok" value={state.stock} />
            <Stat label="Fazla satış" value={oversold} tone={oversold > 0 ? 'bad' : 'good'} />
            <Stat label="Alıcı seti" value={state.status?.buyers ?? '?'} />
          </div>
          <FanoutSummary result={state.result} />
          {state.result.ok && (
            <Tally
              title="Sonuç kodu"
              data={state.result.data.summary.code}
              tones={{ reserved: 'good', sold_out: 'warn' }}
            />
          )}
        </>
      )}
    </Card>
  )
}

// ------------------------------------------------------------------ idempotency
export function IdempotencyLab() {
  const [state, setState] = useState(null)
  const [busy, run] = useAsyncAction()
  const sku = 'SKU-IDEM'

  const start = () =>
    run(async () => {
      await api(`/api/lab/flash-sale/${sku}/reset`, { method: 'POST', body: { stock: 5 }, silent: true })
      const key = uid()
      const request = {
        method: 'POST',
        path: `/api/flash-sale/${sku}/reserve`,
        headers: { 'Idempotency-Key': key },
        body: { user_id: 'idem-user', quantity: 1 },
      }
      const burst = await fanout({ ...request, count: 10 })
      await sleep(300)
      // İstemci "timeout aldım" diyerek aynı anahtarla tekrar dener:
      const retry = await api(request.path, { method: 'POST', body: request.body, headers: request.headers })
      const status = await api(`/api/flash-sale/${sku}`, { silent: true })
      setState({ burst, retry, status: status.data, key })
    })

  return (
    <Card title="Idempotency-Key — çift işlem yok" tag="Redis SET NX">
      <Explain
        what="Aynı Idempotency-Key ile 10 eşzamanlı istek + 1 gecikmeli tekrar. Yalnız ilki işlenir; diğerleri ilk istek hâlâ sürüyorsa 409 (request_in_progress), bitmişse saklanan yanıtı 'Idempotent-Replayed: true' ile alır. Stok yalnız 1 düşer."
        where="Grafana → 'Eşzamanlılık primitifleri' (idempotency new/in_progress/replay)."
      />
      <Button busy={busy} onClick={start}>
        Aynı anahtarla 10+1 istek gönder
      </Button>
      {state && (
        <>
          <div className="stats">
            <Stat label="Kalan stok (5'ten)" value={state.status?.stock ?? '?'} tone={state.status?.stock === 4 ? 'good' : 'bad'} />
            <Stat label="Tekrar denemesi" value={`${state.retry.status}${state.retry.replayed ? ' (replay)' : ''}`} />
          </div>
          <FanoutSummary result={state.burst} />
          {state.burst.ok && (
            <Tally
              title="Sonuç"
              data={{
                ...state.burst.data.summary.code,
                ...(state.burst.data.summary.replayed ? { replayed: state.burst.data.summary.replayed } : {}),
              }}
            />
          )}
        </>
      )}
    </Card>
  )
}

// ------------------------------------------------------------------ rate limit
export function RateLimitLab() {
  const [result, setResult] = useState(null)
  const [busy, run] = useAsyncAction()

  const start = () =>
    run(async () => {
      setResult(await fanout({ path: '/api/lab/rate-limited', count: 30, headers: { 'X-Client-Id': CLIENT_ID } }))
    })

  return (
    <Card title="Dağıtık rate limiter" tag="Redis ZSET + Lua">
      <Explain
        what="Limit: istemci başına 10 istek / 10 sn — TÜM replikalar için ortak sayaç (sliding window, Redis TIME). Pod başına bellek içi limiter replika sayısı kadar gevşerdi."
        where="Yanıt header'ları RateLimit-Remaining / Retry-After; Grafana → ratelimit allowed/limited."
      />
      <Button busy={busy} onClick={start}>
        30 istek gönder
      </Button>
      {result && (
        <>
          <FanoutSummary result={result} />
          {result.ok && (
            <div className="stats">
              <Stat label="İzin verilen" value={result.data.summary.status['200'] || 0} tone="good" />
              <Stat label="429 Too Many Requests" value={result.data.summary.status['429'] || 0} tone="warn" />
            </div>
          )}
        </>
      )}
    </Card>
  )
}

// ------------------------------------------------------------------ distributed lock
export function LockLab() {
  const [state, setState] = useState(null)
  const [busy, run] = useAsyncAction()

  const start = () =>
    run(async () => {
      const result = await fanout({ method: 'POST', path: '/api/lab/jobs/warm-cache', count: 10 })
      let job
      for (let i = 0; i < 20; i += 1) {
        await sleep(500)
        job = await api('/api/lab/jobs/warm-cache', { silent: true })
        if (job.ok && !job.data.running) break
      }
      setState({ result, job })
    })

  const lastRun = state?.job?.data?.last_run
  return (
    <Card title="Dağıtık kilit + fencing token" tag="Redis lock">
      <Explain
        what="'Cache ısıtma' işi aynı anda yalnız bir yerde çalışmalı. 10 eşzamanlı tetikleme iki replikaya dağılır; yalnız kilidi alan çalışır (202), diğerleri 409 alır. Kilit TTL'lidir (sahibi çökerse açılır) ve iş sürerken uzatılır."
        where="Mongo job_runs kaydı yalnız daha büyük fencing token ile güncellenebilir (eski sahibin geç yazması reddedilir)."
      />
      <Button busy={busy} onClick={start}>
        İşi 10 kez eşzamanlı tetikle
      </Button>
      {state && (
        <>
          <FanoutSummary result={state.result} />
          {lastRun && (
            <div className="stats">
              <Stat label="Fencing token" value={lastRun.fence} />
              <Stat label="Isıtılan ürün" value={lastRun.warmed} />
              <Stat label="Çalıştıran replika" value={lastRun.served_by} />
            </div>
          )}
          <ErrorBox result={state.job} />
        </>
      )}
    </Card>
  )
}
