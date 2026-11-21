from app.access_policy import AccessPolicy, highest_role

ROSTER = {"tag": "#CLAN", "members": 1, "memberList": [{"tag": "#ALT", "role": "leader"}]}


def test_multiple_accounts_and_role_changes(tmp_path):
    policy = AccessPolicy(tmp_path / "db")
    assert policy.observe(1,2,["#MAIN","#ALT"],ROSTER,"#CLAN",0) == "member"
    assert highest_role(["#MAIN","#ALT"],ROSTER["memberList"]) == "leader"
    assert highest_role(["#ALT"],[{"tag":"#ALT","role":"admin"}]) == "admin"


def test_confirmation_restart_failure_and_return(tmp_path):
    path = tmp_path / "db"
    policy = AccessPolicy(path)
    assert policy.observe(1,2,["#MAIN"],ROSTER,"#CLAN",0) == "pending"
    policy = AccessPolicy(path)
    assert policy.observe(1,2,["#MAIN"],None,"#CLAN",86400) == "unknown"
    assert policy.observe(1,2,["#MAIN"],ROSTER,"#CLAN",86401) == "pending"
    assert policy.observe(1,2,["#MAIN"],ROSTER,"#CLAN",172801) == "candidate"
    assert policy.observe(1,2,["#ALT"],ROSTER,"#CLAN",172802) == "member"


def test_guests_expiry_group_isolation_and_unlinked(tmp_path):
    p = AccessPolicy(tmp_path / "db")
    p.guest(1,2,3,1000,0)
    assert p.observe(1,2,["#MAIN"],ROSTER,"#CLAN",500) == "guest"
    assert p.observe(2,2,["#MAIN"],ROSTER,"#CLAN",500) == "pending"
    assert p.observe(1,2,["#MAIN"],ROSTER,"#CLAN",1001) == "pending"
    p.guest(1,2,3,None,1002)
    p.revoke(1,2,3,1003)
    assert p.observe(1,2,[],ROSTER,"#CLAN",999999) == "registration"


def test_empty_partial_duplicate_rosters_never_remove(tmp_path):
    p = AccessPolicy(tmp_path / "db")
    for roster in ({}, {"tag":"#CLAN","members":0,"memberList":[]},
                   {**ROSTER,"members":2}, {**ROSTER,"tag":"#OTHER"}):
        assert p.observe(1,2,["#MAIN"],roster,"#CLAN",999999) == "unknown"
