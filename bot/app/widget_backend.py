"""Bounded shared public-data reads; never caches roles, bindings or drafts."""

import asyncio
import copy
import time
from collections import OrderedDict
import httpx
from app.settings import settings


class WidgetBackend:
    def __init__(self, client=None):
        self.client = client
        self.limit = asyncio.Semaphore(4)
        self.pending = {}
        self.cache = OrderedDict()

    async def fetch(self, path, params=None):
        key = (path, tuple(sorted((params or {}).items())))
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= 128:
                raise httpx.PoolTimeout("Public data queue is full")
            task = asyncio.create_task(self.load(key, path, params))
            self.pending[key] = task
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        return copy.deepcopy(await asyncio.shield(task))

    async def load(self, key, path, params):
        try:
            async with self.limit:
                if self.client is None:
                    self.client = httpx.AsyncClient(
                        timeout=12,
                        limits=httpx.Limits(
                            max_connections=4, max_keepalive_connections=4
                        ),
                    )
                response = await self.client.get(
                    settings.backend_url + path, params=params
                )
                response.raise_for_status()
                data = response.json()
                self.cache[key] = (time.monotonic() + 15, data)
                self.cache.move_to_end(key)
                while len(self.cache) > 128:
                    self.cache.popitem(last=False)
                return data
        finally:
            self.pending.pop(key, None)

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.aclose()
