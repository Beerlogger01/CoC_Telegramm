from app.ui import lineup_message, lineup_keyboard


def test_red_opt_out_and_cwl_missed_visible():
    member = {"name": "Member <&>", "tag": "#A", "warPreference": "out",
              "townHallLevel": 17, "role": "admin", "selectionScore": 50,
              "cwlHistory": {"wars": 1}}
    data = {"clanName": "Clan", "mode": "cwl", "recommendedSize": 15,
            "recommendedLineup": [member],
            "categories": {"main": [], "optout": [member], "cwl_review": []},
            "weights": {"strength": 20, "discipline": 35, "quality": 40, "role": 5}}
    assert "КВ/ЛВК" in lineup_message(data, "optout")
    assert "Member &lt;&amp;&gt;" in lineup_message(data, "player", 0)
    keys = str(lineup_keyboard("filters", data=data, captain=True).inline_keyboard)
    assert "lineup:cwl_review:0" in keys
    assert "lineup:bonus:0" in keys
    assert "40%" in lineup_message(data, "weights")


def test_no_old_cwl_telegram_opt_in():
    data = {"mode":"cwl", "clanName":"Test", "categories":{"main":[]}, "recommendedLineup":[]}
    assert "ЛВК из Telegram" not in lineup_message(data,"about")
    assert "НЕ включать" in lineup_message(data,"about")
