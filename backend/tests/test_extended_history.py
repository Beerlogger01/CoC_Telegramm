from app.extended_history import RaidHistory, defense


def test_raids_upsert_restart_and_missing(tmp_path):
    path=str(tmp_path / "db")
    store=RaidHistory(path)
    season={"state":"ended","startTime":"week1","members":[{"tag":"#A","attacks":6,"attackLimit":5,"bonusAttackLimit":1,"capitalResourcesLooted":1200}]}
    store.save("#CLAN",[season,season])
    store=RaidHistory(path)
    row=store.summary("#CLAN","#A")
    assert row["participations"]==1 and row["goldPerAttack"]==200
    assert store.summary("#CLAN","#MISSING")["gold"] is None
    assert row["regularity"] is None


def test_defense_stronger_opponent_does_not_penalize():
    war={"state":"warEnded","clan":{"tag":"#CLAN","members":[{"tag":"#A","townhallLevel":15}]},"opponent":{"tag":"#OTHER","members":[{"tag":"#B","townhallLevel":16,"attacks":[{"defenderTag":"#A","order":1,"stars":3,"destructionPercentage":100}]}]}}
    assert defense([war],"#CLAN")["#A"]["defenseIndex"] is None
    war["opponent"]["members"][0]["townhallLevel"]=15
    war["opponent"]["members"][0]["attacks"][0]["stars"]=1
    assert defense([war],"#CLAN")["#A"]["defenseIndex"]==66.7
