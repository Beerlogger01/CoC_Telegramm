from pathlib import Path
from app.views import WELCOME, welcome_text


def test_welcome_message_uses_html_without_repeated_pinning():
    code = (Path(__file__).parents[1] / "app" / "bot.py").read_text()
    for previous in WELCOME:
        text, chosen = welcome_text("Player", "Lex<&>", "#ABC", previous)
        assert chosen != previous
        assert "Lex&lt;&amp;&gt;" in text and "<code>#ABC</code>" in text
    assert "pin_chat_message(" not in code
    assert "unpin_chat_message(" not in code


def test_start_uses_real_bot_username_not_placeholder():
    code = (Path(__file__).parents[1] / "app" / "bot.py").read_text()
    assert "@bot_username" not in code
    assert "@{context.bot.username}" in code
