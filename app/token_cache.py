import logging

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)


def _cache_key() -> str:
    return f"bff:{settings.environment}:{settings.frontend_id}:{settings.backend_id}:token"


def get_cached_token() -> str | None:
    if not settings.token_cache_enabled:
        return None
    token = get_redis().get(_cache_key())
    logger.info("Redis cache %s for key %s", "HIT" if token else "MISS", _cache_key())
    return token


def store_token(access_token: str, expires_in: int) -> None:
    if not settings.token_cache_enabled:
        return
    ttl = max(expires_in - settings.token_ttl_safety_buffer_seconds, 1)
    get_redis().set(_cache_key(), access_token, ex=ttl)
    logger.info("Stored token in Redis for key %s with TTL %ss", _cache_key(), ttl)
