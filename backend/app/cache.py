"""Bounded in-memory TTL cache for a single-container Home Assistant add-on."""

import asyncio
import copy
import json
import time
from collections import OrderedDict
from typing import Any, Optional


class TTLCache:
    def __init__(self, max_entries: int = 512):
        self.max_entries = max_entries
        self._values = OrderedDict()
        self._lock = asyncio.Lock()
        self._flights = {}
        self.outbound = asyncio.Semaphore(5)

    async def get(self, key: str) -> Optional[str]:
        async with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires <= time.monotonic():
                del self._values[key]
                return None
            self._values.move_to_end(key)
            return value

    async def set(self, key: str, value: str, ex: int = 300) -> None:
        async with self._lock:
            self._values[key] = (time.monotonic() + max(1, ex), value)
            self._values.move_to_end(key)
            now = time.monotonic()
            for old_key, (expires, _) in list(self._values.items()):
                if expires <= now:
                    del self._values[old_key]
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    async def singleflight(self, key, factory):
        task = self._flights.get(key)
        if task is None:

            async def run():
                try:
                    return await factory()
                finally:
                    self._flights.pop(key, None)

            task = asyncio.create_task(run())
            self._flights[key] = task
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        return copy.deepcopy(await asyncio.shield(task))

    async def close(self) -> None:
        tasks = list(self._flights.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._values.clear()


async def get_cached_json(redis: TTLCache, key: str) -> Optional[dict[str, Any]]:
    value = await redis.get(key)
    return json.loads(value) if value is not None else None


async def set_cached_json(
    redis: TTLCache, key: str, value: dict[str, Any], ttl_seconds: int
) -> None:
    await redis.set(key, json.dumps(value), ex=ttl_seconds)
