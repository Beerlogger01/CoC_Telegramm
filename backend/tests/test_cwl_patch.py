import asyncio
import sqlite3

import pytest

from app import coc_client
from app.selection import MODE_WEIGHTS, SelectionStore, score_and_categorize
from app.war_metrics import relative_strength


def member(tag, th, **extra):
    return {"tag": tag, "name": tag, "townHallLevel": th, "warPreference": "in", **extra}


def test_cwl_absolute_th_overcomes_low_th_peer_advantage():
    players = [member("#18A", 18, heroesLevel=300), member("#18B", 18, heroesLevel=600),
               member("#15A", 15, heroesLevel=100), member("#15B", 15, heroesLevel=500)]
    cwl = score_and_categorize(players, MODE_WEIGHTS["cwl"], {}, mode="cwl")
    scores = {p["tag"]: p["selectionScore"] for p in cwl["allPlayers"]}
    assert scores["#18A"] > scores["#15B"]
    war = score_and_categorize(players, MODE_WEIGHTS["war"], {})
    scores = {p["tag"]: p["selectionScore"] for p in war["allPlayers"]}
    assert scores["#15B"] > scores["#18A"]


def test_experience_and_modern_league_change_same_th_ranking():
    players = [member("#LEX", 18, heroesLevel=300, expLevel=280, league="Electro League 32"),
               member("#ALEX", 18, heroesLevel=300, expLevel=250, league="Electro League 31")]
    strengths = relative_strength(players)
    assert strengths["#LEX"]["relativeStrength"] > strengths["#ALEX"]["relativeStrength"]
    players[0]["expLevel"], players[1]["expLevel"] = 250, 280
    players[0]["league"], players[1]["league"] = "Electro League 31", "Electro League 32"
    assert relative_strength(players)["#ALEX"]["relativeStrength"] > relative_strength(players)["#LEX"]["relativeStrength"]


def test_role_is_a_small_bonus_and_never_overrides_opt_out():
    players = [member("#CO", 18, role="coLeader"), member("#MEM", 18, role="member"),
               member("#OUT", 18, role="leader", warPreference="out")]
    result = score_and_categorize(players, MODE_WEIGHTS["cwl"], {}, mode="cwl")
    assert [p["tag"] for p in result["categories"]["main"]] == ["#CO", "#MEM"]
    assert len(result["clanRanking"]) == 3
    assert [p["clanRank"] for p in result["clanRanking"]] == [1, 2, 3]


@pytest.mark.parametrize("size", [15, 20, 25, 30, 35, 40, 45, 50])
def test_regular_war_sizes_and_fixed_cwl(size):
    players = [member(f"#{n}", 18) for n in range(50)]
    war = score_and_categorize(players, MODE_WEIGHTS["war"], {}, size=size)
    cwl = score_and_categorize(players, MODE_WEIGHTS["cwl"], {}, mode="cwl", size=size)
    assert len(war["categories"]["main"]) == size
    assert len(cwl["categories"]["main"]) == 15


def test_old_default_weights_migrate_once_and_custom_values_survive(tmp_path):
    path = str(tmp_path / "selection.db")
    SelectionStore(path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM policy_migrations")
        db.executemany("UPDATE mode_weights SET value=? WHERE mode='cwl' AND metric=?",
                       [(20, "strength"), (35, "discipline"), (40, "quality"), (5, "role")])
    assert SelectionStore(path).weights("cwl") == MODE_WEIGHTS["cwl"]
    custom = SelectionStore(path).set_weight("quality", 45, "cwl")
    assert SelectionStore(path).weights("cwl") == custom
    # Even when no migration has run, a customized previous setup is preserved.
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM policy_migrations")
    assert SelectionStore(path).weights("cwl") == custom


def test_cwl_fetches_other_clans_for_table_but_not_for_history(monkeypatch):
    group = {"state": "inWar", "clans": [{"tag": tag, "name": tag} for tag in ("#P", "#Y", "#L", "#Q")],
             "rounds": [{"warTags": ["#G", "#R"]}] + [{"warTags": ["#0"]}] * 6}
    wars = {"#G": {"state": "warEnded", "startTime": "1", "clan": {"tag": "#P", "stars": 20, "destructionPercentage": 50.0},
                    "opponent": {"tag": "#Y", "stars": 25, "destructionPercentage": 60.0}},
            "#R": {"state": "inWar", "startTime": "1", "clan": {"tag": "#L", "stars": 30, "destructionPercentage": 70.0},
                    "opponent": {"tag": "#Q", "stars": 20, "destructionPercentage": 50.0}}}
    missing = False

    async def fetch(client, cache, key, url):
        if "leaguegroup" in url:
            return group
        tag = "#" + url.rsplit("%23", 1)[1]
        if missing and tag == "#R":
            raise coc_client.NotFoundError()
        return wars[tag]

    monkeypatch.setattr(coc_client.settings, "coc_clan_tag", "#P")
    monkeypatch.setattr(coc_client, "fetch_with_cache", fetch)
    _, own = asyncio.run(coc_client.get_league_wars(None, None))
    assert len(own) == 1
    summary = asyncio.run(coc_client.get_cwl_summary(None, None))
    assert summary["currentPlace"] == 3  # Equal total stars and destruction share third place.
    assert summary["scheduledRounds"] == 7
    assert summary["totalStars"] == 20
    assert summary["standingsComplete"]
    assert len(summary["rounds"]) == 1
    missing = True
    partial = asyncio.run(coc_client.get_cwl_summary(None, None))
    assert not partial["standingsComplete"] and partial["missingWars"] == 1


def test_cwl_duplicate_wars_not_counted_twice_and_future_wars_not_scored():
    ended = {"_warTag": "#G", "state": "warEnded", "startTime": "1",
             "clan": {"tag": "#P", "stars": 40}, "opponent": {"tag": "#Y", "stars": 20}}
    future = {"_warTag": "#R", "state": "preparation", "startTime": "2",
              "clan": {"tag": "#P", "stars": 0}, "opponent": {"tag": "#L", "stars": 0}}
    group = {"state": "inWar", "rounds": [{"warTags": ["#G", "#R"]}]}
    result = coc_client.summarize_cwl(group, [ended, ended, future], "#P")
    assert result["totalStars"] == 50
    assert result["attackStars"] == 40 and result["winBonus"] == 10
    assert result["observedRounds"] == 2


def test_profile_uses_experience_fallback_and_only_home_heroes(monkeypatch):
    async def clan(*args):
        return {"memberList": [member("#P", 18, expLevel=280)]}
    async def profile(*args):
        return {"townHallLevel": 18, "warPreference": "in", "heroes": [
            {"village": "home", "level": 50}, {"village": "builderBase", "level": 35}]}
    async def unavailable(*args):
        raise coc_client.NotFoundError()
    async def league(*args):
        return {"state": "notInWar"}, []
    monkeypatch.setattr(coc_client, "get_clan", clan)
    monkeypatch.setattr(coc_client, "get_player", profile)
    monkeypatch.setattr(coc_client, "get_league_wars", league)
    monkeypatch.setattr(coc_client, "get_regular_war", unavailable)
    monkeypatch.setattr(coc_client, "get_clan_raids", unavailable)
    monkeypatch.setattr(coc_client, "fetch_with_cache", unavailable)
    monkeypatch.setattr(coc_client.settings, "coc_clan_tag", "#P")
    # Client/cache are not contacted because all sources are stubbed.
    data = asyncio.run(coc_client.get_next_war_analysis(None, None, mode="cwl", size=45))
    assert data["recommendedSize"] == 15
    assert data["recommendedLineup"][0]["heroesLevel"] == 50
    assert data["recommendedLineup"][0]["expLevel"] == 280


def test_cwl_bonus_is_awarded_only_after_end_and_destruction_breaks_ties():
    def war(tag, state, ours, enemy, damage, enemy_damage):
        return {"_warTag": tag, "state": state, "startTime": tag,
                "clan": {"tag": "#P", "stars": ours, "destructionPercentage": damage},
                "opponent": {"tag": "#Y", "stars": enemy, "destructionPercentage": enemy_damage}}
    group = {"state": "inWar", "rounds": [{"warTags": ["#G"]}]}
    live = coc_client.summarize_cwl(group, [war("#G", "inWar", 30, 30, 90, 80)], "#P")
    assert live["winBonus"] == 0 and live["totalStars"] == 30
    assert live["currentPlace"] == 1
    ended = coc_client.summarize_cwl(group, [war("#G", "warEnded", 30, 30, 90, 80)], "#P")
    assert ended["winBonus"] == 10 and ended["totalStars"] == 40
    draw = coc_client.summarize_cwl(group, [war("#G", "warEnded", 30, 30, 90, 90)], "#P")
    assert draw["winBonus"] == 0
    assert [row["place"] for row in draw["standings"]] == [1, 1]


def test_legend_tiers_remain_above_modern_numeric_leagues():
    from app.war_metrics import _league_index
    assert _league_index("Legend League I") > _league_index("Legend League II") > _league_index("Legend League III") > _league_index("Electro League 33")
    assert _league_index("Titan 25") == _league_index("Titan League 25")


def test_missing_middle_cwl_war_never_renumbers_later_round(monkeypatch):
    group={'state':'inWar','rounds':[{'warTags':['#P']},{'warTags':['#Y']},{'warTags':['#L']}]+[{'warTags':['#0']}]*4}
    async def fetch(client,cache,key,url):
        if 'leaguegroup' in url: return group
        tag='#'+url.rsplit('%23',1)[-1]
        if tag=='#Y': raise coc_client.NotFoundError()
        return {'state':'warEnded','startTime':tag,'clan':{'tag':'#P','stars':30},'opponent':{'tag':'#Q','stars':20}}
    monkeypatch.setattr(coc_client.settings,'coc_clan_tag','#P')
    monkeypatch.setattr(coc_client,'fetch_with_cache',fetch)
    summary=asyncio.run(coc_client.get_cwl_summary(None,None))
    assert sorted(r['roundNumber'] for r in summary['rounds'])==[1,3]
    assert summary['missingWars']==1 and summary['scheduledRounds']==7


def test_exp_and_role_are_bonuses_without_overriding_attack_strength():
    players=[member('#A',18,heroesLevel=300,heroEquipmentScore=10,expLevel=300,role='leader',league='Electro League 32'),
             member('#B',18,heroesLevel=600,heroEquipmentScore=30,expLevel=200,role='member',league='Electro League 32')]
    result=score_and_categorize(players,MODE_WEIGHTS['cwl'],{},mode='cwl')
    assert result['clanRanking'][0]['tag']=='#B'
    assert result['allPlayers'][0]['strengthCoverage']==100
    role_delta=(result['allPlayers'][0]['scoreComponents']['role']-result['allPlayers'][1]['scoreComponents']['role'])*MODE_WEIGHTS['cwl']['role']/100
    assert 0 < role_delta <= 1
