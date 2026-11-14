import asyncio
from app.cache import TTLCache


def test_singleflight_survives_cancelled_waiter_and_copies_results():
    async def run():
        cache = TTLCache()
        gate = asyncio.Event()
        entered = asyncio.Event()
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            entered.set()
            await gate.wait()
            return {"items": []}

        first = asyncio.create_task(cache.singleflight("clan", factory))
        await entered.wait()
        others = [
            asyncio.create_task(cache.singleflight("clan", factory)) for _ in range(20)
        ]
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        gate.set()
        results = await asyncio.gather(*others)
        assert calls == 1
        results[0]["items"].append("private")
        assert results[1]["items"] == []
        assert not cache._flights
        await cache.close()

    asyncio.run(run())
