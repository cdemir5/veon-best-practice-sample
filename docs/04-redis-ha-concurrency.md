# 4 · Redis — HA, eşzamanlılık ve yönetim

## 4.1 Topoloji: Sentinel

```
           ┌──────────── sentinel-1 ─┐
           │  sentinel-2   sentinel-3│   quorum = 2 (3'ten 2'si "down" demeli)
           └───────┬─────────────────┘
                   │ izler, oylar, terfi ettirir
  redis-master ◄───┴──► redis-replica-1, redis-replica-2   (asenkron replikasyon)
        ▲
        │ master_for("mymaster")  — istemci master adresini SENTINEL'den öğrenir
   uygulama
```

| Karar | Öneri | Neden |
|-------|-------|-------|
| Sentinel sayısı | **3 veya 5** (tek sayı), ayrı node/AZ | Çoğunluk oylaması; tek sentinel'in ağ sorunu failover tetiklemez |
| Veri düğümü | 1 master + ≥2 replica, anti-affinity | Bir düğüm kaybında hâlâ replika kalır |
| İstemci | Doğrudan master IP'sine değil **Sentinel'e** bağlanır | Failover'da adres değişir |
| Hostname | `resolve-hostnames yes`, `announce-hostnames yes`, `replica-announce-ip <host>` | Container/pod IP'si restart'ta değişir |
| Sentinel mi Cluster mı? | Veri tek node belleğine sığıyorsa **Sentinel** | Cluster: yatay bölümleme, ama multi-key işlemler hash-tag ister, `SELECT` yok |

Local yapılandırma: [infra/redis/redis-node.sh](../infra/redis/redis-node.sh), [infra/redis/sentinel.sh](../infra/redis/sentinel.sh).
K8s örneği: [deploy/helm/examples/redis-sentinel.values.yaml](../deploy/helm/examples/redis-sentinel.values.yaml).

## 4.2 İstemci yapılandırması — [client.py](../backend/app/redis_kit/client.py)

```python
retry = Retry(ExponentialWithJitterBackoff(base=0.05, cap=1.0), 5,
              supported_errors=(ConnectionError,))           # ← TimeoutError YOK (aşağıya bakın)
sentinel = Sentinel(endpoints, min_other_sentinels=1,
                    sentinel_kwargs={"password": ..., "socket_timeout": 0.5})
primary = sentinel.master_for("mymaster", password=..., socket_timeout=0.5,
                              socket_connect_timeout=0.5, socket_keepalive=True,
                              health_check_interval=15, retry=retry,
                              max_connections=100, client_name="svc:pod")
replica = sentinel.slave_for("mymaster", ...)                # bayatlığa toleranslı okumalar
```

| Ayar | Neden |
|------|-------|
| **Process başına tek havuz** (lifespan'de kurulur) | İstek başına client = bağlantı fırtınası, TIME_WAIT, yavaşlık |
| `socket_timeout=0.5` | Donmuş master'da istek **asılı kalmaz** (ölçüm: donma senaryosunda istekler 0.5 sn'de hata verip failover sonrası düzeldi) |
| `socket_connect_timeout=0.5` | Erişilemeyen düğümde hızlı başarısızlık |
| Retry yalnız `ConnectionError` | Bağlantı kurulamadıysa komut sunucuya **ulaşmamıştır** → tekrar güvenli. `TimeoutError`'da komut **çalışmış olabilir**; `INCR`, `XADD`, Lua gibi idempotent olmayan komutları körlemesine tekrarlamak çift yazım üretir. |
| Jitter'lı backoff | Failover anında tüm pod'ların aynı milisaniyede yeniden denemesini (thundering herd) önler |
| `health_check_interval` | NAT/LB'nin sessizce kestiği boşta bağlantıları kullanmadan önce yakalar |
| `max_connections` | Pod başına üst sınır; Redis `maxclients` × pod sayısı hesabı yapılmalı |
| `min_other_sentinels=1` | İzole kalmış tek sentinel'in bayat master bilgisini kabul etme |
| `client_name` | `CLIENT LIST`'te hangi pod'un bağlandığı görünür |
| `READONLY` hatası | Failover sonrası eski master'a giden yazma → redis-py bağlantıyı düşürüp Sentinel'e yeniden sorar |
| **Blocking komutlar için ayrı client** | `XREADGROUP BLOCK 2000` / `BLPOP` → `socket_timeout` blok süresinden uzun olmalı; aynı havuzu kullanırsanız her çağrı TimeoutError olur ([worker.py](../backend/app/worker.py) `stream_redis`) |
| `register_script` (EVALSHA) | Failover sonrası yeni master'da script cache boş → `NOSCRIPT` → redis-py otomatik yükler |

## 4.3 Failover: ölçülen kesinti pencereleri

Local stack'te frontend'deki heartbeat (saniyede 1-2 `INCR`) ile ölçülmüştür:

| Senaryo | Komut | Kesinti | Hata tipi |
|---------|-------|---------|-----------|
| Master süreci öldü (TCP RST) | `make failover-hard` | ~**2 sn** | `ConnectionError` (retry ile çoğu istek fark etmedi) |
| Master dondu (ağ bölünmesi, RST yok) | `make failover-freeze` | ~**2–3 sn** | `TimeoutError` (0.5 sn'de hızlı hata) |
| Kontrollü failover | `make failover` | ~**9 sn** yazma reddi | `ResponseError: NOREPLICAS` |

**Neden kontrollü failover daha uzun?** Master'larda `min-replicas-to-write 1` var. Terfi eden yeni master'a
replikalar henüz bağlanıp senkronize olmadan yazmalar `NOREPLICAS` ile reddedilir. Bu bilinçli bir takastır:

| | `min-replicas-to-write 1` (bu repo) | `0` (Redis varsayılanı) |
|-|------|------|
| Failover sonrası yazma penceresi | Daha uzun reddetme | Hemen yazılabilir |
| Split-brain (izole eski master'a yazma) | **Engellenir** (eski master replikasız kalınca yazmayı reddeder) | Yazmalar kabul edilir, birleşmede **kaybolur** |

Uygulama bu pencereyi **tasarımla** karşılar:
- Cache: Redis hatasında `BYPASS` → doğrudan Mongo (servis ayakta).
- Event publisher: 20 sn **zaman bütçeli** retry; bu sürede olaylar bellek kuyruğunda birikir.
  Ölçüm: kontrollü failover sırasında sürekli olay üretiminde **0 kayıp**.
- Kritik yazmalar: `Idempotency-Key` ile istemci güvenle tekrar dener.
- `NOREPLICAS`, `LOADING`, `MASTERDOWN` hataları **reddedilmiş** yazmalardır (çalışmamıştır) → tekrar denemek güvenlidir.

> **Asenkron replikasyon uyarısı:** Master, yazmayı replikalara iletmeden çökerse onaylanmış son yazmalar
> kaybolabilir. Redis bir **cache / koordinasyon** katmanıdır; kaybı kabul edilemeyen veri için kaynak
> MongoDB/RabbitMQ'dur. Gerekirse kritik yazmadan sonra `WAIT 1 100` ile en az bir replikanın onayı beklenebilir.

## 4.4 Anahtar tasarımı — [keys.py](../backend/app/redis_kit/keys.py)

| Kural | Örnek |
|-------|-------|
| `<prefix>:<alan>:<sürüm>:<id>` | `vsa:product:v1:SKU-1` |
| Değer şeması değişince sürüm artır | `v1 → v2`: rolling update'te eski/yeni pod'lar birbirinin cache'ini bozmaz |
| DB index (`SELECT 0/1/2`) yerine prefix | Redis Cluster'da yalnız DB 0 var; ACL'ler prefix ile yazılır (`~vsa:*`) |
| Çok anahtarlı Lua/MULTI'de **hash-tag** | `vsa:{flash:SKU-1}:stock`, `vsa:{flash:SKU-1}:buyers` → aynı slot (test ile doğrulanır) |
| Anahtarda kişisel veri / token YOK | SHA-256/HMAC özeti. **Python `hash()` KULLANMA**: process başına rastgele seed (test: `test_digest_is_stable_across_processes_unlike_builtin_hash`) |
| **Her anahtarın TTL'i var** | cache, kilit, idempotency, rate-limit, alıcı seti … TTL'siz anahtar = bellek sızıntısı |

## 4.5 Bellek ve kalıcılık

| Veri tipi | `maxmemory-policy` | Kalıcılık |
|-----------|--------------------|-----------|
| Saf cache | `allkeys-lru` / `allkeys-lfu` | Gerekmez (`save ""`, AOF kapalı) |
| Stream, idempotency, kilit, sayaç | **`noeviction`** (sessizce silinmesin; dolunca yazma hata verir → alarm) | AOF `everysec` |

Bu repo tek Sentinel kümesinde ikisini birlikte tutar (`noeviction`); **üretimde ayrı instance** önerilir.
`redis_memory_used_bytes / redis_memory_max_bytes > 0.85` alarmı vardır.

## 4.6 Eşzamanlılık desenleri

### a) Cache-aside + stampede koruması — [cache.py](../backend/app/redis_kit/cache.py)

```
GET → hit: dön
    → miss: SET key:lock token NX PX 3000
        ├─ aldı:   tekrar GET (double-check) → Mongo → SET EX ttl±%10 → kilidi bırak (token kontrollü)
        └─ alamadı: 20→200 ms artan aralıkla GET, gelince "COALESCED"; zaman aşımında kendisi yükler
    → Redis hatası: cache'i atla (BYPASS)
```
Ölçüm (2 replika, 50 eşzamanlı istek, 300 ms yavaş kaynak): korumalı **1**, korumasız **50** Mongo okuması.
Ek kurallar: TTL jitter (avalanche), negatif cache (penetration), yazmada **sil** (set etme).

### b) Atomik oku-karar ver-yaz: Lua — [scripts.py](../backend/app/redis_kit/scripts.py)

```lua
local stock = tonumber(redis.call('GET', KEYS[1]))
if stock == nil then return {-2, 0} end
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then return {-1, stock} end
if stock < tonumber(ARGV[2]) then return {0, stock} end
local remaining = redis.call('DECRBY', KEYS[1], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[1])
...
```
Ölçüm (stok 10, 100 eşzamanlı alıcı): Lua **10** satış; naif `GET → kontrol → SET` **100** satış.
Script'ler kısa tutulur (çalışırken Redis başka komut işlemez); tüm anahtarlar `KEYS[]` ile verilir.

Basit durumlar için Lua'ya gerek yok: `INCR`, `SET NX`, `HINCRBY`, `ZADD`, `DECRBY` + sonuç kontrolü zaten atomiktir.
Okuyup Python'da karar verip yazmak gerekiyorsa `WATCH/MULTI/EXEC` (optimistic locking) kullanılabilir.

### c) Idempotency-Key — [idempotency.py](../backend/app/redis_kit/idempotency.py)

`SET idem:<scope>:<sha256(key)> {"state":"in_progress","fp":…} NX EX 30` → işle → sonucu 24 saat sakla.
Aynı anahtar + aynı payload → saklanan yanıt (`Idempotent-Replayed: true`); farklı payload → 422; işlenirken → 409.
5xx sonuçlar saklanmaz (tekrar denenebilir). Ölçüm: 10 eşzamanlı + 1 tekrar → stok yalnız **1** düştü.

### d) Dağıtık kilit + fencing token — [lab.py](../backend/app/api/lab.py)

```python
lock = redis.lock(key, timeout=10, blocking=False, thread_local=False)   # SET NX PX + token
if not await lock.acquire(): return 409
fence = await redis.incr(fence_key)            # her kilit alımında artan sayı
... await lock.reacquire()                     # uzun işte TTL'i uzat; kaybedildiyse LockError
db.job_runs.update_one({"_id": "job", "fence": {"$lt": fence}}, {...}, upsert=True)
```
- TTL'siz kilit = sahibi çökerse sonsuz kilit. TTL var; iş sürerken uzatılır.
- Bırakma **token kontrollü** (başkasının kilidini silmez) — redis-py `Lock` bunu Lua ile yapar.
- **Fencing:** GC duraklaması/ağ kopması yüzünden kilidi kaybetmiş eski sahip geç yazarsa, depolama katmanı
  daha küçük token'ı reddeder. Kilit tek başına doğruluk garantisi vermez (Redis asenkron replikasyon + failover).
- Redlock (çok bağımsız master) bu örnekte gerekmez; doğruluk kritikse fencing veya DB seviyesinde kısıt kullanın.

### e) Dağıtık rate limiter — [rate_limit.py](../backend/app/redis_kit/rate_limit.py)

Sorted-set sliding window, zaman `redis.call('TIME')` (pod saat farkı yok). Sayaç Redis'te olduğu için
**tüm replikalar ortak** limit uygular (ölçüm: 2 replika, tam 10/10 sn). Redis erişilemezse **fail-open**
(+ metrik); güvenlik limitlerinde (login) fail-closed seçilebilir. Kimlik: üretimde doğrulanmış kullanıcı/tenant.

### f) Redis Streams + consumer group

Asenkron pipeline'ın omurgası → [05-async-log-pipeline.md](05-async-log-pipeline.md).

## 4.7 Operasyonel kurallar

| Yap | Yapma |
|-----|-------|
| `SCAN` + `MATCH` + `COUNT` | `KEYS *` (tek thread'i bloklar) |
| Büyük anahtar silmede `UNLINK` | Büyük set/hash'te `DEL` |
| Toplu işlemde pipeline (`transaction=False`) | Döngüde tek tek round-trip |
| Değerleri küçük tut (< 100 KB), hot key'i böl | MB'lık JSON blob, tek sayaçta tüm trafik |
| Uygulama başına ACL kullanıcısı (`~vsa:* +@all -@dangerous`) | Herkes `default` kullanıcı + tek parola |
| TLS (ağ güvenilmezse), parola config dosyasında | Parola komut satırında (`ps`'te görünür) |
| `slowlog`, `latency monitor`, redis_exporter alarmları | Kör çalışmak |
| Session/state güncellemesinde CAS (Lua/`WATCH`) veya versiyon alanı | GET → birleştir → SET (eşzamanlı güncellemede kayıp güncelleme) |

## 4.8 İzleme

- redis_exporter: `redis_up`, `redis_instance_info{role}` (rol zaman çizelgesi paneli), `redis_connected_slaves`,
  `redis_memory_used_bytes`, `redis_commands_processed_total`, `redis_connected_clients`.
- Uygulama: `redis_errors_total{operation,error}`, `cache_requests_total{result="bypass"}` (degrade modda mıyız?).
- Arayüzdeki "Redis HA" kartı: sentinel'lerin gördüğü master, quorum, replika offset farkı, `maxmemory-policy`.

## 4.9 Kubernetes'te Redis HA

- Örnek values: [redis-sentinel.values.yaml](../deploy/helm/examples/redis-sentinel.values.yaml) (Bitnami chart):
  3 düğüm (her pod'da redis + sentinel), `podAntiAffinityPreset: hard`, PVC, PDB, AOF, `min-replicas-to-write`, ServiceMonitor.
- **Bitnami kataloğu 2025'te değişti**: ücretsiz sürümlü imajlar `bitnamilegacy`'ye taşındı. Kurumda onaylı imaj
  kaynağını (registry mirror / Bitnami Secure Images) veya bir operator'ü (ör. OT-Container-Kit redis-operator)
  kullanın ve values anahtarlarını kurduğunuz chart sürümüyle doğrulayın.
- Uygulama tarafı: `config.redis.mode=sentinel`, `sentinels` listesi `redis-node-N.redis-headless.<ns>.svc…:26379`.
