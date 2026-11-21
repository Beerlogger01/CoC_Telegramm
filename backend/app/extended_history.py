"""Observed raid history and defensive outcomes; missing observations stay unknown."""
import json
import sqlite3
from collections import defaultdict
from contextlib import closing


class RaidHistory:
    def __init__(self,path):
        self.path=path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS raid_seasons(clan TEXT,start TEXT,payload TEXT,PRIMARY KEY(clan,start))")

    def save(self,clan,seasons):
        with closing(sqlite3.connect(self.path)) as db, db:
            for season in seasons:
                if season.get("state") == "ended" and season.get("startTime") and isinstance(season.get("members"),list):
                    db.execute("INSERT OR REPLACE INTO raid_seasons VALUES (?,?,?)",(clan,season["startTime"],json.dumps(season)))

    def summary(self,clan,tag):
        with closing(sqlite3.connect(self.path)) as db:
            seasons=[json.loads(r[0]) for r in db.execute("SELECT payload FROM raid_seasons WHERE clan=? ORDER BY start",(clan,))]
        observations=[]
        for season in seasons:
            member=next((m for m in season["members"] if m.get("tag")==tag),None)
            if member:
                observations.append({"start":season["startTime"],"attacks":member.get("attacks"),"available":member.get("attackLimit",0)+member.get("bonusAttackLimit",0) if member.get("attackLimit") is not None else None,"gold":member.get("capitalResourcesLooted")})
        def total(field):
            vals=[r[field] for r in observations]
            return sum(vals) if vals and all(v is not None for v in vals) else None
        attacks,gold=total("attacks"),total("gold")
        return {"observedSeasons":len(seasons),"participations":len(observations),"attacks":attacks,"available":total("available"),"gold":gold,"goldPerAttack":round(gold/attacks,1) if attacks and gold is not None else None,"trend":observations,"regularity":None,"regularityNote":"Membership history is unavailable; absence from a raid is not counted as missed participation"}


def defense(wars,clan_tag):
    rows=defaultdict(list)
    for war in wars:
        if war.get("state") != "warEnded":
            continue
        own,enemy=war.get("clan") or {},war.get("opponent") or {}
        if enemy.get("tag")==clan_tag:
            own,enemy=enemy,own
        if own.get("tag") != clan_tag:
            continue
        defenders={m["tag"]:m for m in own.get("members",[]) if m.get("tag")}
        seen=set()
        for attacker in enemy.get("members",[]):
            for attack in attacker.get("attacks",[]):
                key=(attacker.get("tag"),attack.get("order"),attack.get("defenderTag"))
                if key in seen or attack.get("defenderTag") not in defenders:
                    continue
                seen.add(key)
                target=defenders[attack["defenderTag"]]
                a,d=attacker.get("townhallLevel"),target.get("townhallLevel")
                stars,destruction=attack.get("stars"),attack.get("destructionPercentage")
                # Only equal-TH hits enter the index. Stronger opponents cannot penalize it.
                score=100*(1-stars/3) if a and a==d and type(stars) is int and 0<=stars<=3 else None
                rows[attack["defenderTag"]].append({"attackerTH":a,"defenderTH":d,"stars":stars,"destruction":destruction,"index":score})
    return {tag:{"attacksReceived":len(items),"equalTHAttacks":sum(x["index"] is not None for x in items),"defenseIndex":round(sum(x["index"] for x in items if x["index"] is not None)/sum(x["index"] is not None for x in items),1) if any(x["index"] is not None for x in items) else None,"attacks":items} for tag,items in rows.items()}
