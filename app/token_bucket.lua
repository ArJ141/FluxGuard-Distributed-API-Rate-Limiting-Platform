-- Token Bucket algorithm, atomic via Redis Lua.
--
-- Unlike the sliding window counter (which smooths a rate over a
-- fixed window), a token bucket allows short controlled bursts up to
-- `capacity`, then refills gradually at `refill_rate` tokens/sec.
-- This is a better fit when occasional bursts are fine (e.g. a user
-- rapid-firing a few likes) as long as the SUSTAINED rate stays bounded.
--
-- KEYS[1] = bucket key, e.g. "bucket:{action}:{user_id}"
-- ARGV[1] = capacity        (max tokens the bucket can hold = burst limit)
-- ARGV[2] = refill_rate     (tokens added per second)
-- ARGV[3] = now_ms          (current time in milliseconds)

local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'last_refill_ms')
local tokens = tonumber(bucket[1])
local last_refill_ms = tonumber(bucket[2])

if tokens == nil then
    -- First request from this user/action: start with a full bucket.
    tokens = capacity
    last_refill_ms = now_ms
end

-- Refill based on elapsed time since we last touched this bucket.
local elapsed_seconds = (now_ms - last_refill_ms) / 1000
local refilled = math.min(capacity, tokens + (elapsed_seconds * refill_rate))

local allowed = 0
if refilled >= 1 then
    allowed = 1
    refilled = refilled - 1
end

redis.call('HMSET', KEYS[1], 'tokens', refilled, 'last_refill_ms', now_ms)
-- Cleanup: expire the bucket if unused long enough to fully refill twice over.
local ttl_seconds = math.ceil((capacity / refill_rate) * 2) + 1
redis.call('EXPIRE', KEYS[1], ttl_seconds)

return {allowed, refilled}
