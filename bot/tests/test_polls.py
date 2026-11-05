import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError, BadRequest

from app.polls import PollController, PollStore, duration_seconds, poll_text


def test_votes_change_without_duplicates_and_deadlines_survive_restart(tmp_path):
    path = str(tmp_path / "poll.db")
    store = PollStore(path)
    poll_id = store.create(-100, 1, "КВ завтра?", ["Да", "Нет"], 200)
    store.attach(poll_id, 99)
    assert "принят" in store.vote(poll_id, -100, 99, 7, 0, now=100)
    store.vote(poll_id, -100, 99, 7, 1, now=150)
    store.vote(poll_id, -100, 99, 8, 0, now=150)
    restored = PollStore(path)
    assert restored.get(poll_id)["counts"] == [1, 1]
    assert "завершено" in restored.vote(poll_id, -100, 99, 9, 0, now=200)
    assert "не найдено" in restored.vote(poll_id, -200, 99, 9, 0, now=100)
    assert "не найдено" in restored.vote(poll_id, -100, 98, 9, 0, now=100)
    assert "Нет такого" in restored.vote(poll_id, -100, 99, 9, -1, now=100)
    assert not restored.due(now=199)
    assert "Равенство" in poll_text(restored.due(now=200)[0], result=True)
    restored.mark_announced(poll_id)
    assert not restored.due(now=300)


def test_ambiguous_result_delivery_never_duplicates_on_restart(tmp_path):
    path = str(tmp_path / "poll.db")
    store = PollStore(path)
    poll_id = store.create(-100, 1, "<КВ & ЛВК>", ["A", "B"], 1)
    store.attach(poll_id, 9)
    controller = PollController(store, -100, None, None)
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock(side_effect=TelegramError("timeout after acceptance")))
    context = SimpleNamespace(bot=bot)
    asyncio.run(controller.finish_due(context))
    assert not PollStore(path).due()
    assert "&lt;КВ &amp; ЛВК&gt;" in bot.edit_message_text.call_args.kwargs["text"]
    assert "Никто не проголосовал" in bot.edit_message_text.call_args.kwargs["text"]
    bot.send_message.side_effect = None
    asyncio.run(PollController(PollStore(path), -100, None, None).finish_due(context))
    assert bot.send_message.await_count == 1


def test_result_edit_retries_but_definitive_rejection_releases_send_claim(tmp_path):
    store = PollStore(str(tmp_path / "poll.db"))
    poll_id = store.create(-100, 1, "Question", ["A", "B"], 1)
    store.attach(poll_id, 9)
    controller = PollController(store, -100, None, None)
    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=TelegramError("offline")), send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    asyncio.run(controller.finish_due(context))
    assert store.due() and bot.send_message.await_count == 0
    bot.edit_message_text.side_effect = BadRequest("message is not modified")
    bot.send_message.side_effect = BadRequest("chat not found")
    asyncio.run(controller.finish_due(context))
    assert store.due()
    bot.send_message.side_effect = None
    asyncio.run(controller.finish_due(context))
    assert not store.due()
    asyncio.run(controller.finish_due(context))
    assert bot.send_message.await_count == 2


def test_draft_flow_and_publish_rechecks_leadership(tmp_path):
    access, send = AsyncMock(return_value=True), AsyncMock()
    controller = PollController(PollStore(str(tmp_path / "poll.db")), -100, access, send)
    query = SimpleNamespace(answer=AsyncMock(), data="poll:done")
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1),
                             message=SimpleNamespace(text="Будем играть?"))
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42))))
    asyncio.run(controller.begin(update, context))
    asyncio.run(controller.text(update, context))
    update.message.text = "Да"
    asyncio.run(controller.text(update, context))
    asyncio.run(controller.callback(update, context))
    assert context.user_data["poll_draft"]["stage"] == "options"  # Needs two answers.
    update.message.text = "Нет"
    asyncio.run(controller.text(update, context))
    asyncio.run(controller.callback(update, context))
    assert context.user_data["poll_draft"]["stage"] == "duration"
    update.message.text = "2ч"
    asyncio.run(controller.text(update, context))
    assert context.user_data["poll_draft"]["duration"] == 7200
    query.data = "poll:publish"
    access.return_value = False
    asyncio.run(controller.callback(update, context))
    context.bot.send_message.assert_not_awaited()
    access.return_value = True
    asyncio.run(controller.callback(update, context))
    context.bot.send_message.assert_awaited_once()
    assert controller.store.get(1)["message_id"] == 42
    # A stale/double publish button cannot publish again.
    asyncio.run(controller.callback(update, context))
    context.bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("text,seconds", [("30м", 1800), ("2ч", 7200), ("3д", 259200), ("10", 600)])
def test_duration_units(text, seconds):
    assert duration_seconds(text) == seconds


@pytest.mark.parametrize("text", ["0", "-1", "31д", "завтра"])
def test_duration_rejects_invalid(text):
    with pytest.raises(ValueError):
        duration_seconds(text)
