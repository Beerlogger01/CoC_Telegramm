from app.bot import attacks_used, extract_tag, format_player, normalize_tag


def test_extract_tag_from_message():
    assert extract_tag("мой #2PRGP0L22 вот") == "#2PRGP0L22"
    assert extract_tag("пожалуйста") is None


def test_normalize_tag():
    assert normalize_tag(" 2prgp0l22 ") == "#2PRGP0L22"


def test_attacks_used():
    assert attacks_used({"attacks": [{"stars": 2}]}) == 1
    assert attacks_used({"attacks": 2}) == 2
    assert attacks_used({}) == 0


def test_player_token_setting_default():
    from app.settings import Settings
    assert Settings().require_player_verification is True


def test_player_names_use_safe_html_without_markdown_escapes():
    message = format_player(
        {
            "name": "A_*[Player]<&>",
            "tag": "#2PRGP0L22",
            "clan": {"name": "Clan_(test)"},
        }
    )

    assert "A_*[Player]&lt;&amp;&gt;" in message
    assert "<b>A_*[Player]&lt;&amp;&gt;</b>" in message
