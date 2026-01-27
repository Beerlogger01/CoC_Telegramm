import json
from typing import Any, Optional

from redis.asyncio import Redis


async def get_cached_json(redis: Redis, key: str) -> Optional[dict[str, Any]]:
    cached = await redis.get(key)
    if not cached:
        return None
    return json.loads(cached)


async def set_cached_json(redis: Redis, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
    await redis.set(key, json.dumps(value), ex=ttl_seconds)
