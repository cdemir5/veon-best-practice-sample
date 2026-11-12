import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Button, Card, ErrorBox, Explain, TraceLink, useAsyncAction } from './ui.jsx'

function CacheBadge({ value }) {
  if (!value) return null
  return <span className={`badge cache-${value.toLowerCase()}`}>{value}</span>
}

export default function ProductsCard() {
  const [list, setList] = useState(null)
  const [detail, setDetail] = useState(null)
  const [price, setPrice] = useState('')
  const [busy, run] = useAsyncAction()

  const loadList = () => run(async () => setList(await api('/api/products')))
  const open = (sku) =>
    run(async () => {
      const result = await api(`/api/products/${sku}`)
      setDetail(result)
      if (result.ok) setPrice(String(result.data.price))
    })
  const save = () =>
    run(async () => {
      const sku = detail.data.sku
      const updated = await api(`/api/products/${sku}`, { method: 'PUT', body: { price: Number(price) } })
      if (!updated.ok) return setDetail(updated)
      // Güncellemeden hemen sonra okuma: cache silindiği için MISS, sonraki okuma HIT.
      setDetail(await api(`/api/products/${sku}`))
      loadList()
    })

  useEffect(() => {
    loadList()
  }, [])

  return (
    <Card title="Ürünler — cache-aside" tag="Redis cache">
      <Explain
        what="Okuma: Redis → yoksa Mongo → Redis'e TTL±%10 ile yaz. Güncelleme: önce Mongo, sonra cache anahtarı SİLİNİR ve 'product.updated' olayı asenkron yayınlanır."
        where="Yanıt header'ı X-Cache (HIT/MISS/COALESCED/BYPASS); Grafana → 'Cache sonuçları vs. Mongo okumaları'."
      />
      <div className="row">
        <Button busy={busy} variant="ghost" onClick={loadList}>
          Listeyi yenile
        </Button>
        {list && (
          <span className="muted">
            <CacheBadge value={list.cache} /> {list.ms} ms <TraceLink traceId={list.traceId} />
          </span>
        )}
      </div>
      <ErrorBox result={list} />
      {list?.ok && (
        <ul className="product-list">
          {list.data.items.map((p) => (
            <li key={p.sku}>
              <button className="link" onClick={() => open(p.sku)}>
                {p.sku}
              </button>
              <span>{p.name}</span>
              <span className="num">{p.price.toLocaleString('tr-TR')} ₺</span>
              <span className="muted">v{p.version}</span>
            </li>
          ))}
        </ul>
      )}
      {detail && (
        <div className="detail">
          <ErrorBox result={detail} />
          {detail.ok && (
            <>
              <div className="row">
                <strong>{detail.data.name}</strong>
                <CacheBadge value={detail.cache} />
                <span className="muted">
                  {detail.ms} ms · {detail.servedBy}
                </span>
                <TraceLink traceId={detail.traceId} />
              </div>
              <div className="row">
                <input value={price} onChange={(e) => setPrice(e.target.value)} type="number" min="1" />
                <Button busy={busy} onClick={save}>
                  Fiyatı güncelle
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  )
}
