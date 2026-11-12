"""Lua script'leri — "oku → karar ver → yaz" adımlarını ATOMİK yapar.

Neden Lua?
  * GET + kontrol + SET üç ayrı round-trip'tir; arada başka istek araya girer
    (race condition → flash-sale'de fazla satış). Lua script Redis'te tek seferde,
    araya komut girmeden çalışır.
  * Tek round-trip → daha düşük gecikme.

Kurallar:
  * Script'ler `register_script()` ile kaydedilir → EVALSHA kullanılır,
    NOSCRIPT gelirse (ör. failover sonrası yeni master'da cache yok) redis-py
    script'i otomatik yükleyip tekrar dener.
  * Script'in dokunduğu TÜM anahtarlar KEYS[] ile verilir (Cluster uyumu).
  * Script'ler kısa tutulur: çalışırken Redis başka komut işlemez.
  * Zaman için redis.call('TIME') — uygulama pod'ları arası saat farkı etkilemez.
"""

# Dönüş: {kod, kalan_stok}
#   1 = rezerve edildi, 0 = stok tükendi, -1 = kullanıcı zaten aldı, -2 = kampanya yok
FLASH_SALE_RESERVE = """
local stock = tonumber(redis.call('GET', KEYS[1]))
if stock == nil then
  return {-2, 0}
end
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then
  return {-1, stock}
end
local qty = tonumber(ARGV[2])
if stock < qty then
  return {0, stock}
end
local remaining = redis.call('DECRBY', KEYS[1], qty)
redis.call('SADD', KEYS[2], ARGV[1])
-- Alıcı seti, stok anahtarıyla aynı ömre sahip olsun (TTL'siz anahtar bırakma).
local ttl = redis.call('PTTL', KEYS[1])
if ttl > 0 then
  redis.call('PEXPIRE', KEYS[2], ttl)
end
return {1, remaining}
"""

# Sliding-window-log rate limiter (sorted set).
# KEYS[1]=zset  ARGV[1]=pencere_ms  ARGV[2]=limit  ARGV[3]=benzersiz üye eki
# Dönüş: {izin(1/0), kalan, retry_after_ms}
RATE_LIMIT_SLIDING_WINDOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
  redis.call('ZADD', KEYS[1], now, now .. '-' .. ARGV[3])
  redis.call('PEXPIRE', KEYS[1], window)
  return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local retry_after = window
if oldest[2] then
  retry_after = window - (now - tonumber(oldest[2]))
end
return {0, 0, retry_after}
"""
