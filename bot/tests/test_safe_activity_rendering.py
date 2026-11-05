"""Telegram names are untrusted markup, so rankings must be render-safe."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest

from app.bot import format_top_players, send_or_edit_message
from app.navigation import navigator


def test_player_names_escape_html_metacharacters():
    sample = {"mostActive": [
        {"name": "A_<Ace>&", "townHallLevel": 18, "donations": 100, "warAttacks": 2},
        {"name": "⭐King*_", "townHallLevel": 17, "donations": 10, "warAttacks": 1}
    ], "leastActive": [{"name": "CoC_#2", "donations": 0, "warAttacks": 0}]}
    result = format_top_players(sample)
    assert "A_&lt;Ace&gt;&amp;" in result
    assert "CoC_#2" in result
    assert "<Ace>" not in result
    assert "не время последнего входа" in result


def test_ranking_bounds_telegram_message_length():
    large = {"mostActive": [{"name": "Name_<>&" * 80} for _ in range(50)],
             "leastActive": [{"name": "Name_<>&" * 80} for _ in range(50)]}
    assert len(format_top_players(large)) <= 4000


def test_callback_retries_with_plain_text_after_parse_entities_bad_request():
    edit = AsyncMock(side_effect=[BadRequest("Can't parse entities: can't find end of the entity"),
                                  SimpleNamespace(message_id=1)])
    navigator.reset()
    target = SimpleNamespace(message_id=1, edit_text=edit)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=1),
                             message=None, callback_query=SimpleNamespace(message=target))
    result = asyncio.run(send_or_edit_message(update, "Invalid *markdown_", parse_mode=ParseMode.MARKDOWN))
    assert result.message_id == 1
    assert edit.await_count == 2
    assert edit.await_args.kwargs["parse_mode"] is None


def test_other_telegram_bad_request_is_not_hidden():
    edit = AsyncMock(side_effect=BadRequest("Chat not found"))
    navigator.reset()
    target = SimpleNamespace(message_id=1, edit_text=edit)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=1),
                             message=None, callback_query=SimpleNamespace(message=target))
    with pytest.raises(BadRequest):
        asyncio.run(send_or_edit_message(update, "Text", parse_mode=ParseMode.HTML))
    assert edit.await_count == 1


def test_non_callback_uses_same_fallback():
    navigator.reset()
    reply = AsyncMock(side_effect=[BadRequest("Can't parse entities"), SimpleNamespace(message_id=1)])
    message = SimpleNamespace(reply_text=reply)
    update = SimpleNamespace(message=message,effective_message=message,callback_query=None,
                             effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(id=1))
    assert asyncio.run(send_or_edit_message(update, "Hello <test>", parse_mode=ParseMode.HTML)).message_id == 1
    assert reply.await_count == 2
