from datetime import datetime, timezone

from app.selection import MODE_WEIGHTS, SelectionStore, score_and_categorize

NOW = datetime(2026,11,5,tzinfo=timezone.utc)


def test_independent_mode_weights_are_persistent(tmp_path):
    path=str(tmp_path/"policy.db")
    db=SelectionStore(path)
    assert sum(db.weights("war").values()) == 100
    assert sum(db.weights("cwl").values()) == 100
    assert db.weights("war") != db.weights("cwl")
    db.set_weight("quality",50,mode="cwl")
    assert SelectionStore(path).weights("cwl")["quality"] == 50
    assert SelectionStore(path).weights("war") == MODE_WEIGHTS["war"]


def test_cwl_missed_round_warns_without_automatic_season_ban():
    member={"tag":"#A","name":"A","townHallLevel":18,"warPreference":"in",
            "role":"coLeader","missedThisCWL":True}
    cwl=score_and_categorize([member],MODE_WEIGHTS["cwl"],{},mode="cwl",now=NOW)
    war=score_and_categorize([member],MODE_WEIGHTS["war"],{},mode="war",now=NOW)
    assert len(cwl["categories"]["cwl_review"])==1
    assert len(cwl["categories"]["main"])==1
    assert cwl["allPlayers"][0]["eligibility"] == "candidate"
    assert any("решение за руководством" in reason for reason in cwl["allPlayers"][0]["reasons"])
    assert not war["categories"]["cwl_review"]
    assert len(war["categories"]["main"])==1


def test_red_war_preference_is_hard_gate_both_modes():
    member={"tag":"#A","name":"A","townHallLevel":18,"warPreference":"out",
            "role":"leader","relativeStrength":100}
    for mode in ("war","cwl"):
        result=score_and_categorize([member],MODE_WEIGHTS[mode],{},mode=mode,now=NOW)
        assert not result["categories"]["main"]
        assert len(result["categories"]["optout"])==1


def test_three_fully_missed_wars_trigger_review_not_kick():
    member={"tag":"#A","name":"A","townHallLevel":18,"warPreference":"in",
            "missedWarsInARow":3}
    result=score_and_categorize([member],MODE_WEIGHTS["war"],{},now=NOW)
    assert len(result["categories"]["kick"])==1
    assert result["categories"]["main"]  # Review only, human final decision.
