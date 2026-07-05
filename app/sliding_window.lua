-- Sliding Window Counter algorithm, run atomically inside Redis.
--
-- KEYS[1] = current window key   e.g. "ratelimit:{client_id}:{current_window}"
-- KEYS[2] = previous window key  e.g. "ratelimit:{client_id}:{previous_window}"
-- ARGV[1] = limit (max requests allowed per window)
-- ARGV[2] = window_size_seconds
-- ARGV[3] = elapsed_ms_into_current_window (how far we are into the current window)
--
-- Why "sliding window counter" and not a simple fixed window?
-- A fixed window (e.g. reset every 60s on the clock) lets a client burst
-- 2x the limit right at the boundary (limit requests at 0:59, then limit
-- again at 1:00 -- 2x limit in ~1 second). The sliding window counter
-- estimates the true rate by weighting the previous window's count based
-- on how much of it "overlaps" with the current sliding view.

local current_count = tonumber(redis.call('GET', KEYS[1]) or "0")
local previous_count = tonumber(redis.call('GET', KEYS[2]) or "0")

local limit = tonumber(ARGV[1])
local window_size = tonumber(ARGV[2])
local elapsed_ms = tonumber(ARGV[3])

-- weight = how much of the previous window still "counts" toward now.
-- If we're 10% into the current window, we still count 90% of the
-- previous window's requests -- this is what makes it "sliding" rather
-- than a hard reset.
local weight = (window_size * 1000 - elapsed_ms) / (window_size * 1000)
local estimated_count = (previous_count * weight) + current_count

if estimated_count >= limit then
    -- Deny. Return 0 (denied) and the estimated count for debugging/headers.
    return {0, estimated_count}
end

-- Allow: atomically increment current window's counter.
-- This INCR happens in the SAME atomic script as the check above --
-- that's the whole point. No other request can sneak in between
-- "check" and "increment" because Lua scripts run to completion
-- before Redis processes anything else.
local new_count = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], window_size * 2) -- cleanup: expire old windows

return {1, estimated_count + 1}
