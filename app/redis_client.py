from functools import lru_cache

import redis

from app.config import settings


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        ssl=settings.redis_ssl,
        password=settings.redis_password,
        decode_responses=True,
    )
