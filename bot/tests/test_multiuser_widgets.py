"""Deterministic interleavings. Delays are gates, not assumptions about CPU speed."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import httpx
import pytest
from telegram.error import NetworkError
from app.sessions import SessionUpdateProcessor
from app.widgets import Widgets
from app.widget_store import WidgetStore, StaleWidget
from app.widget_backend import WidgetBackend
from app.settings import settings
from ui_harness import Harness, OWNER, GROUP


def update(user, chat=None):
    return NS(
        api_kwargs={}, effective_chat=NS(id=chat or user), effective_user=NS(id=user)
    )


def q(token, rev=0, owner=7, message=10):
    return {
        "id": str(time.monotonic_ns()),
        "from": {"id": owner},
        "chat": {"id": owner, "type": "private"},
        "message_id": message,
        "type": {"type": "data"},
        "data": f"{token}:{rev}:0",
    }


def test_twenty_users_progress_while_one_is_blocked():
    async def run():
        proc = SessionUpdateProcessor()
        gate = asyncio.Event()
        entered = asyncio.Event()
        order = []
        latencies = []

        async def slow():
            entered.set()
            await gate.wait()
            order.append(1)

        async def same():
            order.append(2)

        async def other(start):
            latencies.append((time.perf_counter() - start) * 1000)

        first = asyncio.create_task(proc.process_update(update(1), slow()))
        await entered.wait()
        second = asyncio.create_task(proc.process_update(update(1), same()))
        start = time.perf_counter()
        await asyncio.wait_for(
            asyncio.gather(
                *(proc.process_update(update(i), other(start)) for i in range(2, 22))
            ),
            2,
        )
        assert len(latencies) == 20 and order == []
        # Same person in another chat is also independent.
        await proc.process_update(update(1, -100), other(start))
        gate.set()
        await asyncio.gather(first, second)
        assert order == [1, 2] and not proc.sessions
        print("20 independent users dispatch ms: max=", round(max(latencies[:20]), 3))

    asyncio.run(run())


def test_private_group_screens_and_wizards_are_isolated(monkeypatch, tmp_path):
    async def run():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            await h.text("/menu")
            private = h.screen.copy()
            h.context.user_data["plan_title"] = "war"
            h.chat = GROUP
            await h.text("/menu")
            group = h.screen.copy()
            assert "plan_title" not in h.context.user_data
            h.chat = OWNER
            assert h.context.user_data["plan_title"] == "war"
            assert h.screen["message_id"] == private["message_id"]
            await h.click("menu_wars")
            h.chat = GROUP
            assert h.screen["message_id"] == group["message_id"]
            await h.click("menu_ratings")
            h.user = 202
            await h.text("/menu")
            other = h.screen["message_id"]
            h.user = OWNER
            assert h.screen["message_id"] != other
            assert not h.errors
        finally:
            await h.shutdown()

    asyncio.run(run())


def test_screen_limit_migration_expiry_and_recoverable_delivery(tmp_path):
    path = tmp_path / "db"
    s = WidgetStore(path)
    tokens = [s.create(7) for _ in range(6)]
    with pytest.raises(StaleWidget):
        s.begin(q(tokens[0]))
    for i, t in enumerate(tokens[1:]):
        s.publish(t, 0, {"0": {"s": "war"}}, 10 + i)
    t = tokens[1]
    s.begin(q(t))
    p = s.prepare(t, 1, {"s": "war"}, '{"q":1}', {"0": {"s": "home"}})
    restored = WidgetStore(path)
    # Lost edit response: same immutable delivery is replayable after restart.
    assert restored.begin(q(t))[-1] == p
    assert restored.delivered(t, 1, p["actions"])
    assert not restored.delivered(t, 1, p["actions"])
    assert restored.begin(q(t))[-1] == p
    with pytest.raises(PermissionError):
        restored.begin(q(t, owner=8))
    # Another live message of the same owner works independently.
    assert restored.begin(q(tokens[2], message=11))[1] == 1
    with sqlite3.connect(path) as db:
        db.execute("UPDATE widget_screens SET expires=0 WHERE token=?", (t,))
    with pytest.raises(StaleWidget):
        restored.begin(q(t))


def test_two_widgets_two_users_network_retry_and_backend_error(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "db", NS(get_user_accounts=lambda *a: []))
        gate = asyncio.Event()
        arrived = 0
        both = asyncio.Event()

        async def fetch(*args):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both.set()
            await gate.wait()
            return {"name": "Clan", "members": 45}

        w.fetch = fetch
        bot = NS(_post=AsyncMock(return_value={"message_id": 10}))
        tasks = [
            asyncio.create_task(
                w.open(
                    NS(
                        effective_user=NS(id=i), effective_chat=NS(id=i, type="private")
                    ),
                    NS(bot=bot),
                )
            )
            for i in (7, 8)
        ]
        await asyncio.wait_for(both.wait(), 2)
        gate.set()
        assert all(await asyncio.gather(*tasks))
        data = [json.loads(c.args[1]["chat_data"]) for c in bot._post.call_args_list]
        assert data[0]["n"] != data[1]["n"]
        w.fetch = AsyncMock(return_value={"state": "notInWar"})
        token = data[0]["n"]
        owner = bot._post.call_args_list[0].args[1]["chat_id"]
        edit = []
        fail = True

        async def post(method, payload, **kw):
            nonlocal fail
            if method == "editMessageWidgetData":
                edit.append(payload["chat_data"])
                if fail:
                    fail = False
                    raise NetworkError("not logged secret")
            return True

        bot._post = post
        await w.query(q(token, owner=owner), bot)
        await w.query(q(token, owner=owner), bot)
        assert len(edit) == 2 and edit[0] == edit[1] and w.fetch.call_count == 1
        await w.close()

    asyncio.run(run())


@pytest.mark.parametrize("status", [502, 504])
def test_backend_failure_keeps_retry_navigation(monkeypatch, tmp_path, status):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "db", NS(get_user_accounts=lambda *a: []))
        t = w.store.create(7)
        w.store.publish(t, 0, {"0": {"s": "war"}}, 10)
        response = httpx.Response(
            status, request=httpx.Request("GET", "https://backend.invalid/war")
        )
        w.fetch = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "secret body", request=response.request, response=response
            )
        )
        bot = NS(_post=AsyncMock(return_value=True))
        await w.query(q(t), bot)
        data = json.loads(
            next(
                c.args[1]["chat_data"]
                for c in bot._post.call_args_list
                if c.args[0] == "editMessageWidgetData"
            )
        )
        assert data["b"] == [["Повторить", "0"], ["Штаб", "1"]]
        assert "secret" not in json.dumps(data)
        await w.close()

    asyncio.run(run())


def test_shared_cache_coalesces_and_limits_ten_profiles():
    async def run():
        gate = asyncio.Event()
        full = asyncio.Event()
        active = 0
        peak = 0
        calls = []

        async def handle(request):
            nonlocal active, peak
            calls.append(str(request.url))
            active += 1
            peak = max(peak, active)
            if active == 4:
                full.set()
            await gate.wait()
            active -= 1
            return httpx.Response(200, json={"value": len(calls)})

        backend = WidgetBackend(
            httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        tasks = [
            asyncio.create_task(backend.fetch("/player/" + str(i))) for i in range(10)
        ]
        await asyncio.wait_for(full.wait(), 2)
        assert len(calls) == 4
        gate.set()
        await asyncio.gather(*tasks)
        assert peak == 4 and len(calls) == 10
        calls.clear()
        values = await asyncio.gather(
            *(backend.fetch("/next-war", {"mode": "war"}) for _ in range(20))
        )
        assert len(calls) == 1
        values[0]["value"] = -1
        assert values[1]["value"] != -1
        await backend.close()

    asyncio.run(run())


def test_sqlite_busy_does_not_block_event_loop(tmp_path):
    async def run():
        w = Widgets(tmp_path / "db", NS(get_user_accounts=lambda *a: []))
        db = sqlite3.connect(w.store.path)
        db.execute("BEGIN IMMEDIATE")
        started = asyncio.Event()
        real = w.store.create
        loop = asyncio.get_running_loop()

        def create(owner):
            loop.call_soon_threadsafe(started.set)
            return real(owner)

        w.store.create = create
        task = asyncio.create_task(w.db("create", 7))
        await asyncio.wait_for(started.wait(), 2)
        # Lock is still held, yet main loop runs and can release it.
        assert not task.done()
        db.rollback()
        db.close()
        assert await asyncio.wait_for(task, 2)
        await w.close()

    asyncio.run(run())


@pytest.mark.parametrize("screen", ["players", "rating", "donations"])
def test_ten_rows_all_pages_and_utf8_budget(tmp_path, screen):
    async def run():
        w = Widgets(tmp_path / "db", NS(get_user_accounts=lambda *a: []))
        players = [
            {
                "tag": f"#P{i}",
                "name": ("👨‍👩‍👧‍👦ЖОченьДлинныйНик" * 3) + str(i),
                "townHallLevel": 18,
                "selectionScore": 99.123456,
                "donations": 123456,
            }
            for i in range(45)
        ]
        w.fetch = AsyncMock(
            return_value={"memberList": players, "clanRanking": players}
        )
        counts = []
        for page in range(5):
            raw, actions = await w.data({"s": screen, "p": page}, 7, "a" * 12, 99999)
            data = json.loads(raw)
            counts.append(len(data["l"]))
            assert len(raw.encode()) <= 1024
            for row in data["l"]:
                assert actions[row[3]]["back"]["p"] == page
                assert row[0].endswith("…")
        assert counts == [10, 10, 10, 10, 5]
        await w.close()

    asyncio.run(run())


def test_all_eight_cwl_clans_fit(tmp_path):
    async def run():
        w = Widgets(tmp_path / "db", NS(get_user_accounts=lambda *a: []))
        w.fetch = AsyncMock(
            return_value={
                "standings": [
                    {"name": "Очень длинный клан" * 4, "place": i + 1, "stars": 280}
                    for i in range(8)
                ]
            }
        )
        raw, _ = await w.data({"s": "cwl"}, 7, "b" * 12, 1)
        assert len(json.loads(raw)["l"]) == 8 and len(raw.encode()) <= 1024
        await w.close()

    asyncio.run(run())


def test_two_leaders_edit_one_plan_without_lost_update(tmp_path):
    from app.operations import WarPlanStore, PlanConflict
    from test_plan_store import create
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = WarPlanStore(str(tmp_path / "db"))
    plan_id = create(store)
    gate = Barrier(2)

    def change(actor):
        gate.wait()
        try:
            store.change(plan_id, 1, actor, tag="#1")
            return True
        except PlanConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(change, [1, 2])) == 1
    assert store.get(plan_id)["revision"] == 2


def test_same_owner_two_widgets_have_independent_navigation(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "db", NS(get_user_accounts=lambda *a: []))
        tokens = [w.store.create(7), w.store.create(7)]
        for i, t in enumerate(tokens):
            w.store.publish(t, 0, {"0": {"s": "war" if i == 0 else "cwl"}}, 10 + i)
        w.fetch = AsyncMock(return_value={"state": "notInWar"})
        bot = NS(_post=AsyncMock(return_value=True))
        await asyncio.gather(
            *(w.query(q(t, message=10 + i), bot) for i, t in enumerate(tokens))
        )
        rendered = {
            c.args[1]["message_id"]: json.loads(c.args[1]["chat_data"])
            for c in bot._post.call_args_list
            if c.args[0] == "editMessageWidgetData"
        }
        assert rendered[10]["t"] != rendered[11]["t"]
        assert rendered[10]["n"] == tokens[0] and rendered[11]["n"] == tokens[1]
        await w.close()

    asyncio.run(run())


def test_slow_telegram_delivery_does_not_block_another_owner(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "db", NS(get_user_accounts=lambda *a: []))
        proc = SessionUpdateProcessor()
        tokens = [w.store.create(i) for i in (7, 8)]
        for i, t in enumerate(tokens):
            w.store.publish(t, 0, {"0": {"s": "war"}}, 10)
        w.fetch = AsyncMock(return_value={"state": "notInWar"})
        gate = asyncio.Event()
        entered = asyncio.Event()
        answered = []

        async def post(method, payload, **kwargs):
            if method == "editMessageWidgetData" and payload["chat_id"] == 7:
                entered.set()
                await gate.wait()
            if method == "answerWidgetQuery":
                answered.append(payload["widget_query_id"])
            return True

        bot = NS(_post=post)
        one = q(tokens[0])
        two = q(tokens[1], owner=8)
        first = asyncio.create_task(proc.process_update(update(7), w.query(one, bot)))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(proc.process_update(update(8), w.query(two, bot)), 2)
        assert two["id"] in answered and one["id"] not in answered
        gate.set()
        await first
        await w.close()

    asyncio.run(run())


def test_sqlite_old_schema_is_additively_migrated(tmp_path):
    path = tmp_path / "db"
    with sqlite3.connect(path) as db:
        db.executescript(
            'CREATE TABLE widget_screens(token TEXT PRIMARY KEY,owner INTEGER NOT NULL,message INTEGER,revision INTEGER NOT NULL,actions TEXT NOT NULL,expires REAL NOT NULL); CREATE TABLE unrelated(value TEXT); INSERT INTO unrelated VALUES ("keep");'
        )
        db.execute(
            "INSERT INTO widget_screens VALUES (?,?,?,?,?,?)",
            ("old", 7, 10, 0, json.dumps({"0": {"s": "war"}}), time.time() + 60),
        )
    s = WidgetStore(path)
    assert s.begin(q("old"))[3] == {"s": "war"}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM unrelated").fetchone()[0] == "keep"
