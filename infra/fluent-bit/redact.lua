-- Best-effort maskeleme (VEON Alloy pipeline'ındaki loki.process "redact" ile aynı fikir).
-- Uygulama zaten maskeler; bu, gözden kaçan kayıtlar için ikinci savunma hattıdır.
local patterns = {
  { "[Bb]earer%s+[%w%._~%+/=%-]+", "Bearer [REDACTED]" },
  { "(://)[^/@%s]*:[^/@%s]*@", "%1[REDACTED]@" },
}

local function scrub(value)
  if type(value) ~= "string" then
    return value
  end
  for _, rule in ipairs(patterns) do
    value = string.gsub(value, rule[1], rule[2])
  end
  return value
end

function redact(tag, timestamp, record)
  local changed = false
  for key, value in pairs(record) do
    local cleaned = scrub(value)
    if cleaned ~= value then
      record[key] = cleaned
      changed = true
    end
  end
  if changed then
    return 1, timestamp, record
  end
  return 0, timestamp, record
end
