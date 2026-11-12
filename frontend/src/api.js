// API istemcisi: korelasyon header'ları + yanıttan gözlemlenebilirlik bilgisi.
//
// Tarayıcıdan OTLP'ye span GÖNDERİLMEZ (VEON politikası: public OTLP ingress yok).
// Bunun yerine her isteğe X-Request-ID eklenir; backend yanıtta X-Trace-Id döner ve
// arayüz bunu Grafana/Tempo bağlantısına çevirir.

export const config = {
  grafanaUrl: 'http://localhost:3000',
  prometheusUrl: 'http://localhost:9090',
  kibanaUrl: 'http://localhost:5601',
  jaegerUrl: 'http://localhost:16686',
  ...(window.__APP_CONFIG__ || {}),
}

// crypto.randomUUID yalnız güvenli bağlamda (https/localhost) var; yedek üretici.
export function uid() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID()
  const bytes = window.crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

export const CLIENT_ID = (() => {
  try {
    let id = localStorage.getItem('vsa-client-id')
    if (!id) {
      id = uid()
      localStorage.setItem('vsa-client-id', id)
    }
    return id
  } catch {
    return uid()
  }
})()

const traceListeners = new Set()
export function onTrace(listener) {
  traceListeners.add(listener)
  return () => traceListeners.delete(listener)
}

export async function api(path, { method = 'GET', body, headers = {}, silent = false } = {}) {
  const requestId = uid()
  const started = performance.now()
  let response
  try {
    response = await fetch(path, {
      method,
      headers: {
        Accept: 'application/json',
        'X-Request-ID': requestId,
        'X-Client-Id': CLIENT_ID,
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (error) {
    return { ok: false, status: 0, data: { code: 'network_error', message: String(error) }, requestId }
  }
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = { raw: text }
  }
  const result = {
    ok: response.ok,
    status: response.status,
    data,
    ms: Math.round(performance.now() - started),
    requestId,
    traceId: response.headers.get('X-Trace-Id'),
    cache: response.headers.get('X-Cache'),
    servedBy: response.headers.get('X-Served-By'),
    replayed: response.headers.get('Idempotent-Replayed') === 'true',
  }
  if (!silent && result.traceId) traceListeners.forEach((listener) => listener(result))
  return result
}

// Sunucu tarafı eşzamanlı istek üretici (tarayıcının host başına ~6 bağlantı sınırını aşar).
export function fanout({ method = 'GET', path, count, headers, body }) {
  return api('/api/lab/fanout', { method: 'POST', body: { method, path, count, headers, body } })
}

export function traceUrl(traceId) {
  const panes = {
    a: {
      datasource: 'tempo',
      queries: [{ refId: 'A', datasource: { type: 'tempo', uid: 'tempo' }, queryType: 'traceql', query: traceId }],
      range: { from: 'now-1h', to: 'now' },
    },
  }
  return `${config.grafanaUrl}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify(panes))}`
}

// Jaeger UI'da doğrudan trace görünümü (jaegerUrl boşsa bağlantı gösterilmez).
export function jaegerTraceUrl(traceId) {
  return config.jaegerUrl ? `${config.jaegerUrl}/trace/${traceId}` : null
}

export function logsUrl(needle) {
  const panes = {
    a: {
      datasource: 'loki',
      queries: [{ refId: 'A', datasource: { type: 'loki', uid: 'loki' }, expr: `{job="fluent-bit"} |= "${needle}"` }],
      range: { from: 'now-1h', to: 'now' },
    },
  }
  return `${config.grafanaUrl}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify(panes))}`
}
