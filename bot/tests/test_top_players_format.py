"""Telegram formatting regression based on the actual user traceback."""
import asyncio
from html.parser import HTMLParser
from types import SimpleNamespace
from app.navigation import navigator

from telegram.constants import ParseMode
from telegram.error import BadRequest

from app.bot import format_top_players, send_or_edit_message


class StrictHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i"}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag


def test_ranking_special_character_names_produce_balanced_html():
    names = ["A_*[Player]", "Ace <Boss> & Co", "ab_u_007__", "Rock <i>not html</i>"]
    payload = {
        "mostActive": [{"name": name, "donations": 15} for name in names],
        "leastActive": [{"name": "X & Y < Z", "warAttacks": 1}],
    }
    markup = format_top_players(payload)
    assert "Ace &lt;Boss&gt; &amp; Co" in markup
    assert "X &amp; Y &lt; Z" in markup
    assert "Rock &lt;i&gt;not html&lt;/i&gt;" in markup
    parser = StrictHTML()
    parser.feed(markup)
    parser.close()
    assert not parser.stack
    assert len(markup) < 4096


def test_very_long_player_names_cannot_truncate_markup():
    name = "<>&" * 2500
    payload = {"mostActive": [{"name": name} for _ in range(10)],
               "leastActive": [{"name": name} for _ in range(10)]}
    msg = format_top_players(payload)
    parser = StrictHTML()
    parser.feed(msg)
    parser.close()
    assert not parser.stack
    assert len(msg) <= 4096


def test_parse_error_retries_as_unformatted_text_without_crashing():
    class Message:
        def __init__(self):
            self.modes = []
        async def reply_text(self, text, parse_mode, reply_markup):
            self.modes.append(parse_mode)
            if parse_mode is not None:
                raise BadRequest("Can't parse entities: can't find end of the entity")
            return SimpleNamespace(message_id=1)
    class Update:
        message = Message()
        effective_message = message
        effective_user = SimpleNamespace(id=1)
        effective_chat = SimpleNamespace(id=1)
        callback_query = None
    navigator.reset()
    update = Update()
    result = asyncio.run(send_or_edit_message(update, "*broken", parse_mode=ParseMode.MARKDOWN))
    assert result.message_id == 1
    assert update.message.modes == [ParseMode.MARKDOWN, None]
