"""Failed-login limiter. State lives in Redis, so every API instance shares it."""
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings
from app.errors import AppError

log = logging.getLogger(__name__)

# INCR + EXPIRE in one atomic script: no race between instances, window starts at 1st failure.
_INCR_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return c
"""


def _key(email: str) -> str:
    return f"login_fail:{email.lower()}"


async def ensure_login_allowed(redis: Redis, email: str) -> None:
    try:
        count = await redis.get(_key(email))
        if count is None or int(count) < settings.login_max_failures:
            return
        ttl = await redis.ttl(_key(email))
    except RedisError:  # fail open: password check + argon2 cost still protect the account
        log.exception("rate limiter unavailable")
        return
    raise AppError(429, "rate_limited", "Too many failed logins, try later",
                       headers={"Retry-After": str(max(ttl, 1))})


async def record_login_failure(redis: Redis, email: str) -> None:
    try:
        await redis.eval(_INCR_LUA, 1, _key(email), settings.login_window_seconds)
    except RedisError:
        log.exception("rate limiter unavailable")
