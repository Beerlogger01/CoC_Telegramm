import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from telegram.error import BadRequest, RetryAfter, TimedOut

from app.settings import settings
from app.widget_store import WidgetStore
from app.widgets import Widgets, diagnostic


def test_widgets_command_preserves_both_owners_and_restart(tmp_path):
    path = tmp_path / "widgets.db"
    store = WidgetStore(path)
    tokens = [store.create(owner) for owner in (7, 7, 8)]
    for token in tokens:
        store.publish(token, 0, {"0": {"s": "home"}}, 10)
    store.preference(7, "widgets")
    store.preference(8, "widgets")
    store = WidgetStore(path)
    with store.connect() as db:
        assert {r[0] for r in db.execute("SELECT token FROM widget_screens")} == set(tokens)
    store.preference(7, "classic")
    with store.connect() as db:
        assert db.execute("SELECT token FROM widget_screens").fetchall() == [(tokens[2],)]


def test_failed_send_explains_cause_without_affecting_other_owner(tmp_path, monkeypatch, caplog):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "widgets.db", SimpleNamespace(get_user_accounts=lambda *a: []))
        w.fetch = AsyncMock(return_value={"name": "Clan"})
        entered, release = asyncio.Event(), asyncio.Event()

        async def post(method, data, **kwargs):
            if data["chat_id"] == 7:
                entered.set()
                await release.wait()
                raise BadRequest("sensitive-server-response")
            return {"message_id": 12}

        bot = SimpleNamespace(_post=post, send_message=AsyncMock())

        def update(owner):
            return SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=owner),
                                   effective_user=SimpleNamespace(id=owner))

        first = asyncio.create_task(w.open(update(7), SimpleNamespace(bot=bot)))
        await entered.wait()
        assert await w.open(update(8), SimpleNamespace(bot=bot))
        release.set()
        assert not await first
        notice = bot.send_message.call_args.kwargs
        assert notice["chat_id"] == 7
        assert "sendWidget / TELEGRAM_API_REJECTED" in notice["text"]
        assert "sensitive-server-response" not in notice["text"] + caplog.text
        with w.store.connect() as db:
            assert db.execute("SELECT message FROM widget_screens WHERE owner=8").fetchone() == (12,)
        await w.close()

    asyncio.run(run())


def test_failed_notice_still_returns_to_classic_menu(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "widgets.db", SimpleNamespace(get_user_accounts=lambda *a: []))
        w.fetch = AsyncMock(side_effect=TimeoutError())
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
        update = SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=7),
                                 effective_user=SimpleNamespace(id=7))
        assert not await w.open(update, SimpleNamespace(bot=bot))
        await w.close()

    asyncio.run(run())


def test_diagnostics_distinguish_sqlite_errors_and_telegram_limits():
    assert diagnostic(TimedOut()) == "TIMEOUT"
    assert diagnostic(RetryAfter(4)) == "TELEGRAM_RATE_LIMITED"
    busy = sqlite3.OperationalError("redacted")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    assert diagnostic(busy) == "SQLITE_BUSY"
    assert diagnostic(sqlite3.OperationalError("missing table")) == "SQLITE_ERROR"


def test_clan_forbidden_reports_access_problem_then_recovers(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(settings, "widgets_enabled", True)
        w = Widgets(tmp_path / "widgets.db", SimpleNamespace(get_user_accounts=lambda *a: []))
        response = httpx.Response(403, request=httpx.Request("GET", "http://backend/clan"))
        failure = httpx.HTTPStatusError("sensitive-body", request=response.request, response=response)
        w.fetch = AsyncMock(side_effect=[failure, {"name": "Clan"}])
        bot = SimpleNamespace(send_message=AsyncMock(), _post=AsyncMock(return_value={"message_id": 15}))
        update = SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=7),
                                 effective_user=SimpleNamespace(id=7))
        assert not await w.open(update, SimpleNamespace(bot=bot))
        text = bot.send_message.call_args.kwargs["text"]
        assert "BACKEND_ACCESS_DENIED" in text
        assert "внешний IP" in text
        assert "sensitive-body" not in text
        bot._post.assert_not_called()
        assert await w.open(update, SimpleNamespace(bot=bot))
        await w.close()

    asyncio.run(run())
