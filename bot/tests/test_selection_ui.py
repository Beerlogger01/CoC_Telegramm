from app.ui import lineup_message, lineup_keyboard


def _roster():
    members = [
        {"tag": "#AAA", "name": "Alpha", "townHallLevel": 18, "selectionScore": 97.2,
         "warPreference": "in", "cwlPreference": "out", "lastSeenSource": "unavailable",
         "history": {"wars": 5, "attacksUsed": 8, "attacksPossible": 10}},
        {"tag": "#BBB", "name": "Beta", "townHallLevel": 17, "selectionScore": 80,
         "warPreference": "in", "cwlPreference": "in", "lastSeenHoursAgo": 75,
         "reasons": ["Ручная отметка: давно не заходил"]},
    ]
    return {"clanName":"Test", "mode":"cwl", "observedWarCount":5,
            "recommendedSize":1, "recommendedLineup":members,
            "categories":{"main":[members[0]],"bench":[],"inactive":[members[1]],
                          "kick":[members[1]],"promotion":[members[0]],"bonus":[]},
            "weights":{"combat":35,"reliability":25,"performance":20,
                       "participation":10,"activity":10}}


def test_weight_table_and_mode():
    data=_roster()
    text=lineup_message(data,"weights")
    assert "35%" in text and "100%" in text and "ЛВК" in text
    assert "НЕ" in lineup_message(data,"about")


def test_manual_last_seen_is_distinct_from_api():
    data=_roster()
    assert "по ручной отметке" in lineup_message(data,"inactive").lower()
    assert "Неизвестно (API не передаёт)" in lineup_message(data,"player",0)


def test_moderation_categories_hidden_without_captain():
    data=_roster()
    ordinary=str(lineup_keyboard("filters", data=data).inline_keyboard)
    captain=str(lineup_keyboard("filters", data=data,captain=True).inline_keyboard)
    assert "lineup:kick:0" not in ordinary
    assert "lineup:kick:0" in captain
