from app.profile_ui import (
    highest_role, profile_text, profile_keyboard, detail_text, detail_keyboard,
    progress_text, progress_keyboard,
)


def fixture():
    return [{"name":"Lex<&>", "tag":"#A", "role":"admin", "townHallLevel":18,
             "warPreference":"in","leagueName":"Titan","trophies":5300,
             "donations":200,"warStars":500,"heroes":[{"name":"King","level":95}]},
            {"name":"Alt_2", "tag":"#B", "role":"coLeader", "townHallLevel":16,
             "warPreference":"out","leagueName":"Master","trophies":2500,
             "donations":80,"warStars":300}]


def test_highest_role_from_any_verified_account():
    players = fixture()
    assert highest_role(players) == "coLeader"
    players += [{"tag":"#C", "role":"leader"}]
    assert highest_role(players) == "leader"


def test_profile_uses_names_and_game_stats_not_just_tags():
    text = profile_text(fixture(), "#A")
    assert "Lex&lt;&amp;&gt;" in text
    assert "Alt_2" in text
    assert "Соруководитель" in text
    assert "TH16" in text
    assert "Донат" in text
    markup = str(profile_keyboard(fixture()).inline_keyboard)
    assert "acct:view:0" in markup and "acct:view:1" in markup


def test_detail_navigation_and_safe_html():
    profile = fixture()[0]
    msg = detail_text(profile, True)
    assert "Lex&lt;&amp;&gt;" in msg and "King" in msg
    assert "acct:progress:0" in str(detail_keyboard(0,True).inline_keyboard)
    assert "acct:primary:0" not in str(detail_keyboard(0,True).inline_keyboard)


def test_progress_selects_alt_and_period_without_invented_data():
    data = {"snapshotDay":"2026-11-05","historyAvailable":False,"snapshotCount":1}
    assert "только один" in progress_text(data,"Alt_2",7)
    controls = str(progress_keyboard(fixture(),1,30).inline_keyboard)
    assert "acct:progress:1" in controls
    assert "growth:days:30" in controls


def test_progress_and_accounts_are_fully_paginated():
    from app.profile_ui import progress_changes
    profiles=[{'tag':f'#{n}','name':f'Player {n}'} for n in range(25)]
    assert 'acct:view:24' in str(profile_keyboard(profiles,99))
    assert 'Player 24' in profile_text(profiles,None,99)
    data={'historyAvailable':True,'changes':{'heroEquipment':{f'Item <{n}>':{'from':1,'to':2} for n in range(45)}}}
    assert len(progress_changes(data))==45
    assert 'Item &lt;44&gt;' in progress_text(data,'Lex',30,2)
    assert 'Item &lt;0&gt;' not in progress_text(data,'Lex',30,2)
    assert 'growth:page:2' in str(progress_keyboard(profiles,24,30,1,3))
