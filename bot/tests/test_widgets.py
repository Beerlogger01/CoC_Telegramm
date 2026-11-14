import asyncio
import json
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from app.widget_store import WidgetStore, StaleWidget
from app.widget_template import WIDGET_HTML, encode_state
from app.widgets import Widgets
from app.widget_views import view
from app.settings import settings


def query(token, revision=0, owner=7, message=10):
    return {
        "id": "q1",
        "from": {"id": owner},
        "chat": {"id": owner, "type": "private"},
        "message_id": message,
        "type": {"type": "data"},
        "data": f"{token}:{revision}:0",
    }


def test_limits_and_template():
    assert len(WIDGET_HTML.encode()) < 32768
    assert len(zlib.compress(WIDGET_HTML.encode(), 9)) < 4096
    assert "fetch(" not in WIDGET_HTML and "innerHTML" not in WIDGET_HTML
    with pytest.raises(ValueError):
        encode_state({"x": "я" * 600})


def test_persistent_capabilities_and_replay(tmp_path):
    path = tmp_path / "data.db"
    s = WidgetStore(path)
    token = s.create(7)
    s.publish(token, 0, {"0": {"s": "war"}}, 10)
    for owner, message in [(8, 10), (7, 11)]:
        with pytest.raises(StaleWidget):
            s.claim(query(token, owner=owner, message=message))
    s = WidgetStore(path)
    assert s.claim(query(token))[3] == {"s": "war"}
    with pytest.raises(StaleWidget):
        s.claim(query(token))
    s.preference(7, "classic")
    assert WidgetStore(path).mode(7) == "classic"
    with pytest.raises(StaleWidget):
        s.claim(query(token, 1))


def test_groups_financial_and_forged_actions_rejected(tmp_path):
    s = WidgetStore(tmp_path / "data.db")
    token = s.create(7)
    s.publish(token, 0, {"0": {"s": "war"}}, 10)
    q = query(token)
    q["chat"]["type"] = "group"
    with pytest.raises(StaleWidget):
        s.claim(q)
    q = query(token)
    q["type"]["type"] = "sign_data"
    with pytest.raises(StaleWidget):
        s.claim(q)
    q = query(token)
    q["data"] = token + ":0:evil"
    with pytest.raises(StaleWidget):
        s.claim(q)
    assert s.claim(query(token))[2] == 7


def test_ptb_preserves_widget_update():
    q = query("abc")
    update = Update.de_json({"update_id": 1, "widget_query": q}, None)
    assert update.api_kwargs["widget_query"] == q
    assert update.effective_user is None  # Router must use Telegram's query.from.


def test_open_group_and_fallback(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "db", SimpleNamespace(get_user_accounts=lambda *a: []))
        bot = SimpleNamespace(_post=AsyncMock())
        u = SimpleNamespace(
            effective_chat=SimpleNamespace(type="group", id=-7),
            effective_user=SimpleNamespace(id=7),
        )
        assert not await w.open(u, SimpleNamespace(bot=bot))
        bot._post.assert_not_called()
        u.effective_chat = SimpleNamespace(type="private", id=7)
        w.fetch = AsyncMock(side_effect=TimeoutError())
        assert not await w.open(u, SimpleNamespace(bot=bot))
        bot._post.assert_not_called()

    asyncio.run(run())


def test_send_query_edit_answer_and_restart(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        bindings = SimpleNamespace(get_user_accounts=lambda *a: [])
        w = Widgets(tmp_path / "db", bindings)
        w.fetch = AsyncMock(
            return_value={"name": "Клан", "members": 20, "clanLevel": 10}
        )
        bot = SimpleNamespace(_post=AsyncMock(return_value={"message_id": 10}))
        u = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private", id=7),
            effective_user=SimpleNamespace(id=7),
        )
        assert await w.open(u, SimpleNamespace(bot=bot))
        method, payload = bot._post.call_args.args
        assert method == "sendWidget" and payload["public_data"] == ""
        d = json.loads(payload["chat_data"])
        token = d["n"]
        w = Widgets(tmp_path / "db", bindings)
        w.fetch = AsyncMock(return_value={"state": "notInWar"})
        await w.query(query(token), bot)
        assert bot._post.call_args.args[0] == "answerWidgetQuery"
        assert any(
            c.args[0] == "editMessageWidgetData" for c in bot._post.call_args_list
        )
        count = sum(
            c.args[0] == "editMessageWidgetData" for c in bot._post.call_args_list
        )
        await w.query(query(token), bot)
        assert (
            sum(c.args[0] == "editMessageWidgetData" for c in bot._post.call_args_list)
            == count + 1
        )

    asyncio.run(run())


def test_account_access_is_rechecked():
    async def run():
        storage = SimpleNamespace(get_user_accounts=lambda *a: [])
        fetch = AsyncMock(return_value={"memberList": []})
        with pytest.raises(ValueError):
            await view({"s": "profile", "tag": "#PRIVATE"}, 7, storage, 0, fetch)
        assert fetch.call_count == 1

    asyncio.run(run())


def test_real_ptb_transport_and_router(tmp_path, monkeypatch):
    from telegram.ext import ApplicationBuilder, TypeHandler
    from telegram.request import BaseRequest
    from app.widgets import dispatch_widget

    class Transport(BaseRequest):
        def __init__(self):
            self.calls = []

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def do_request(self, url, method, request_data=None, **kw):
            name = url.rsplit("/", 1)[-1]
            payload = request_data.parameters
            self.calls.append((name, payload))
            result = (
                {"id": 999, "is_bot": True, "first_name": "Test", "username": "TestBot"}
                if name == "getMe"
                else {"message_id": 10}
                if name == "sendWidget"
                else True
            )
            return 200, json.dumps({"ok": True, "result": result}).encode()

    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        transport = Transport()
        app = ApplicationBuilder().token("999:test").request(transport).build()
        w = Widgets(tmp_path / "db", SimpleNamespace(get_user_accounts=lambda *a: []))
        w.fetch = AsyncMock(return_value={"name": "Clan", "members": 20})
        app.bot_data["widgets"] = w
        app.add_handler(TypeHandler(Update, dispatch_widget), group=-3)
        await app.initialize()
        try:
            u = SimpleNamespace(
                effective_chat=SimpleNamespace(type="private", id=7),
                effective_user=SimpleNamespace(id=7),
            )
            assert await w.open(u, SimpleNamespace(bot=app.bot))
            data = json.loads(transport.calls[-1][1]["chat_data"])
            q = query(data["n"])
            q["from"].update(is_bot=False, first_name="Test")
            await app.process_update(
                Update.de_json({"update_id": 42, "widget_query": q}, app.bot)
            )
            for _ in range(100):
                if any(n == "answerWidgetQuery" for n, _ in transport.calls):
                    break
                await asyncio.sleep(0.01)
            assert [n for n, _ in transport.calls][-2:] == [
                "editMessageWidgetData",
                "answerWidgetQuery",
            ]
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_view_state_size_and_multiple_accounts():
    async def run():
        accounts = [
            SimpleNamespace(coc_player_tag="#P0Y"),
            SimpleNamespace(coc_player_tag="#P0Q"),
        ]
        storage = SimpleNamespace(get_user_accounts=lambda *a: accounts)
        fetch = AsyncMock(
            return_value={"name": "Клан", "memberList": [], "members": 20}
        )
        for route in [
            {"s": x}
            for x in ["home", "accounts", "players", "war", "cwl", "rating", "tools"]
        ]:
            data, buttons = await view(route, 7, storage, 0, fetch)
            data.update(
                n="a" * 12, q=999, b=[[t, str(i)] for i, (t, _) in enumerate(buttons)]
            )
            assert len(encode_state(data).encode()) <= 1024
        data, buttons = await view({"s": "accounts"}, 7, storage, 0, fetch)
        assert {r["tag"] for r in data["_actions"]} == {"#P0Y", "#P0Q"}

    asyncio.run(run())


def test_widget_reply_is_not_queued_behind_legacy_handler():
    from app.widgets import WidgetUpdateProcessor

    async def run():
        processor = WidgetUpdateProcessor()
        gate = asyncio.Event()
        entered = asyncio.Event()
        replied = asyncio.Event()

        async def slow():
            entered.set()
            await gate.wait()

        async def fast():
            replied.set()

        first = asyncio.create_task(
            processor.process_update(SimpleNamespace(api_kwargs={}), slow())
        )
        await entered.wait()
        await asyncio.wait_for(
            processor.process_update(
                SimpleNamespace(api_kwargs={"widget_query": {"chat":{"id":7},"from":{"id":7}}}), fast()
            ),
            0.1,
        )
        assert replied.is_set() and not first.done()
        gate.set()
        await first

    asyncio.run(run())
