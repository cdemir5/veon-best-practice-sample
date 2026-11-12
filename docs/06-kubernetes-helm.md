# 6 · Docker imajları ve Helm chart

## İmajlar

| İmaj | Taban | Özellikler |
|------|-------|------------|
| `veon-sample/backend` | `python:3.12.8-slim` (multi-stage) | venv build aşamasında; root olmayan `10001`; read-only root FS ile çalışır; **tek imaj üç rol**: API (varsayılan CMD), `python -m app.worker`, `python -m app.migrate` |
| `veon-sample/frontend` | `node:22.12.0-alpine` → `nginxinc/nginx-unprivileged:1.27.3-alpine` | Vite build; 8080, uid 101; çalışma zamanı config'i (`/config.js`) env'den → **aynı imaj her ortamda** |

Her ikisi de `--read-only --cap-drop ALL --security-opt no-new-privileges` ile test edilmiştir
(yazılabilir alanlar: backend `/tmp`; frontend `/tmp` ve `/etc/nginx/conf.d`).

Kurallar: sürüm sabitleme (latest yok), üretimde **digest** pin (`image.*.digest`), bağımlılık güncellemesi
Renovate/Dependabot PR'ı + CI testleriyle, imaj taraması (Trivy/Grype) CI'da.

## Chart: `deploy/helm/veon-sample-app`

```
templates/
  configmap.yaml           # gizli olmayan ayarlar (REDIS_MODE, REDIS_SENTINELS …)
  tracing-configmap.yaml   # global.tracing.enabled → OTEL_* (VEON agentic-tracing-config deseni)
  backend.yaml             # Deployment + Service (http, metrics) + HPA
  worker.yaml              # Deployment + headless metrics Service + (opsiyonel) KEDA ScaledObject
  frontend.yaml            # Deployment + Service
  pdb.yaml                 # 3 bileşen için PodDisruptionBudget
  ingress.yaml             # tek giriş → frontend (nginx /api'yi backend'e proxy'ler)
  observability.yaml       # ServiceMonitor + PrometheusRule + Grafana dashboard ConfigMap
  networkpolicy.yaml       # ingress yönü: yalnız gereken akışlar
  migration-job.yaml       # pre-install/pre-upgrade hook: index + opsiyonel seed
  serviceaccount.yaml      # automountServiceAccountToken: false
  tests/test-readyz.yaml   # helm test
```

Doğrulama: `make helm-lint` (helm lint + template) ve kubeconform ile **66/66** kaynak (CRD'ler dahil:
ServiceMonitor, PrometheusRule, ScaledObject, TriggerAuthentication) şema doğrulamasından geçti.

## Kurulum

```bash
# 1) Gizli değerler (chart DIŞINDA — tercihen External Secrets / Vault)
kubectl create ns veon-ns
kubectl -n veon-ns create secret generic veon-sample-secrets \
  --from-literal=redis-password='…' \
  --from-literal=sentinel-password='…' \
  --from-literal=mongo-uri='mongodb://user:pass@mongo-0.mongo:27017,mongo-1.mongo:27017/?replicaSet=rs0&authSource=admin'

# 2) (Yoksa) Redis Sentinel kümesi — bkz. examples/redis-sentinel.values.yaml

# 3) Render edip inceleyin, sonra kurun
helm template vsa deploy/helm/veon-sample-app -n veon-ns -f deploy/helm/examples/values-stage.yaml > review.yaml
helm upgrade --install vsa deploy/helm/veon-sample-app -n veon-ns \
  -f deploy/helm/examples/values-stage.yaml --atomic --timeout 10m
helm -n veon-ns test vsa
```

## Önemli values

| Değer | Varsayılan | Açıklama |
|-------|-----------|----------|
| `global.tracing.enabled/endpoint/samplingRatio/environment` | kapalı / VEON Alloy / `0.1` | VEON uygulama chart'larıyla **aynı arayüz**; açılınca `-tracing` ConfigMap'i oluşur, checksum ile pod'lar yenilenir |
| `config.redis.mode` | `sentinel` | `standalone` için `config.redis.url` |
| `config.redis.sentinels` | Bitnami `redis-node-N` adresleri | Tek sayıda, ayrı node'larda |
| `config.redis.keyPrefix` | `vsa` | Ortamlar Redis paylaşıyorsa ortam adı ekleyin |
| `config.labEndpointsEnabled` | `false` | **Üretimde kapalı** |
| `secrets.existingSecret` | `veon-sample-secrets` | Anahtarlar `secrets.keys.*` |
| `backend.autoscaling` | 2–6, CPU %70 | `scaleDown.stabilizationWindowSeconds: 300` |
| `worker.keda.enabled` | `false` | Lag'e göre 1–8 replika (`redis-sentinel-streams`) |
| `metrics.serviceMonitor.labels` | `{}` | Kümedeki Prometheus'un seçicisine uyan label |
| `metrics.grafanaDashboard.labels` | `grafana_dashboard: "1"` | Grafana sidecar'ı |
| `networkPolicy.prometheusNamespaces` | `[observability]` | 9100'e erişebilen namespace'ler |
| `image.*.digest` | boş | Üretimde digest pin |

## Kubernetes best-practice'leri (chart'ta uygulananlar)

| Konu | Uygulama |
|------|----------|
| Güvenlik | `runAsNonRoot`, uid 10001/101, `readOnlyRootFilesystem`, `drop: [ALL]`, `seccompProfile: RuntimeDefault`, SA token mount yok |
| Gizli değerler | Yalnız `secretKeyRef`; values'ta parola yok; ConfigMap'te secret yok |
| Probe'lar | startup + liveness (`/healthz`, bağımlılıksız) + readiness (`/readyz`, Mongo); worker'da heartbeat exec probe |
| Graceful shutdown | `preStop: sleep 5` (endpoint yayılımı) → SIGTERM → uvicorn açık istekleri bitirir → event kuyruğu boşaltılır → span'ler flush; worker mevcut batch'i bitirip ACK'ler |
| Rolling update | `maxUnavailable: 0`, `maxSurge: 25%` |
| Dayanıklılık | PDB (`maxUnavailable: 1`), `topologySpreadConstraints` (hostname + zone) |
| Ölçek | Backend HPA (CPU); worker KEDA (stream lag) — kuyruk tüketicisini CPU'ya göre ölçeklemek yanıltıcıdır |
| Config değişikliği | `checksum/config`, `checksum/tracing` annotation'ları → yalnız ilgili değişiklikte rollout |
| Migration | Hook Job (`backoffLimit: 3`, `activeDeadlineSeconds`, başarılıysa silinir, başarısızsa inceleme için kalır) |
| Ağ | NetworkPolicy: API'ye yalnız aynı release, metriklere yalnız `observability`, frontend'e yalnız ingress controller |
| Gözlemlenebilirlik | ServiceMonitor, PrometheusRule (namespace + job filtreli), dashboard ConfigMap; loglar stdout (toplayıcı kümede) |
| Log toplayıcı | Pod'lara `app: <release>-<bileşen>` label'ı → VEON Alloy discovery'si ile uyumlu |

## Prometheus / Grafana entegrasyonu (VEON observability bundle ile)

1. ServiceMonitor ve PrometheusRule'un Prometheus tarafından seçildiğini doğrulayın (label/namespace seçicileri).
2. Exemplar için Prometheus CR: `spec.enableFeatures: [exemplar-storage]`.
3. Dashboard sidecar'ı yoksa `kubectl get cm vsa-veon-sample-app-dashboard -o jsonpath='{.data.*}'` çıktısını Grafana'ya import edin.
4. Loki datasource'unda `otel_trace_id` derived field'ı (VEON README adım 7) zaten tanımlı olmalı.
5. Dashboard'daki **Log stream selector** değişkenini küme label'larına göre ayarlayın, örn. `{namespace="veon-ns", app=~"vsa-.*"}`.

## Jaeger seçeneği

Uygulama chart'ı Jaeger kurmaz ve uygulamada değişiklik gerekmez. İki yol var:

1. **Önerilen (fan-out):** `global.tracing.endpoint` VEON Alloy'unda kalır; Alloy config'ine ikinci bir
   `otelcol.exporter.otlp "jaeger"` eklenip batch çıkışına bağlanır (local [config.alloy](../infra/alloy/config.alloy) ile aynı).
   Tempo standart backend olarak kalır, Jaeger ek arayüz olur.
2. **Yalnız Jaeger:** `global.tracing.endpoint: http://<jaeger-collector>.<ns>.svc.cluster.local:4318`
   (Jaeger v2 OTLP HTTP). Bu durumda VEON Alloy'daki gizlilik allowlist'i devreden çıkar; tercih edilmez.

Arayüzdeki "Jaeger ↗" bağlantıları için `frontend.publicUrls.jaeger` değerini verin. Kümede Jaeger için
kalıcı depolama (Elasticsearch/OpenSearch) ve UI'a kimlikli erişim gerekir; bellek içi depolama yalnız local içindir.

## EFK seçeneği

Fluent Bit DaemonSet → Elasticsearch: [examples/fluent-bit-efk.values.yaml](../deploy/helm/examples/fluent-bit-efk.values.yaml)
(tail + CRI multiline, kubernetes filtresi `Merge_Log On`, filesystem buffer, ES TLS + kimlik, `Generate_ID`).
Aynı namespace'i Alloy de izliyorsa **çift log** oluşur — geçişi namespace bazında yapın.
