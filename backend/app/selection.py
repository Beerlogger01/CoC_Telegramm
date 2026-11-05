"""Evidence-based KV/CWL selection. Never automatically kick, promote or award medals.

The public CoC API has warPreference but NO last-online time. Manual captain
observations are clearly separated from public data and never guessed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.war_metrics import promotion_score, relative_strength

# Old 0.2.1 draft interface is retained for backwards compatibility with tests
# and existing SQLite settings; actual KV/CWL weights are stored separately.
DEFAULT_WEIGHTS = {
    "combat": 30.0, "reliability": 25.0, "performance": 25.0,
    "participation": 10.0, "activity": 10.0,
}
MODE_WEIGHTS = {
    "war": {"strength": 20.0, "discipline": 35.0, "quality": 35.0, "role": 10.0},
    "cwl": {"strength": 55.0, "discipline": 20.0, "quality": 20.0, "role": 5.0},
}
ROLE_SCORE = {"leader": 65.0, "coLeader": 60.0, "admin": 55.0, "member": 50.0}
PREFS = ("in", "out", "unknown")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        return dt.astimezone(timezone.utc) if dt and dt.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


class SelectionStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=10) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS preferences (
                clan_tag TEXT NOT NULL, player_tag TEXT NOT NULL,
                cwl_opt TEXT NOT NULL DEFAULT 'unknown',
                last_seen_manual TEXT NULL, last_seen_reporter INTEGER NULL,
                updated TEXT NOT NULL, PRIMARY KEY(clan_tag,player_tag)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS weights (
                metric TEXT PRIMARY KEY, value REAL NOT NULL
                CHECK(value >= 0 AND value <= 100)
            )""")
            conn.executemany("INSERT OR IGNORE INTO weights VALUES(?,?)",
                             list(DEFAULT_WEIGHTS.items()))
            conn.execute("""CREATE TABLE IF NOT EXISTS mode_weights (
                mode TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
                PRIMARY KEY(mode,metric), CHECK(value >= 0 AND value <= 100)
            )""")
            for mode, values in MODE_WEIGHTS.items():
                conn.executemany("INSERT OR IGNORE INTO mode_weights VALUES(?,?,?)",
                                 [(mode, metric, value) for metric, value in values.items()])

            # Migrate only the previous defaults, once. Preserve customized weights.
            conn.execute("CREATE TABLE IF NOT EXISTS policy_migrations (name TEXT PRIMARY KEY)")
            migration = "cwl_absolute_strength_v1"
            if not conn.execute("SELECT 1 FROM policy_migrations WHERE name=?", (migration,)).fetchone():
                current = dict(conn.execute("SELECT metric,value FROM mode_weights WHERE mode='cwl'"))
                if current == {"strength": 20., "discipline": 35., "quality": 40., "role": 5.}:
                    conn.executemany("UPDATE mode_weights SET value=? WHERE mode='cwl' AND metric=?",
                                     [(v, k) for k, v in MODE_WEIGHTS["cwl"].items()])
                conn.execute("INSERT INTO policy_migrations VALUES(?)", (migration,))

    def weights(self, mode: str | None = None) -> dict[str, float]:
        with sqlite3.connect(self.path, timeout=10) as conn:
            if mode is None:
                rows = dict(conn.execute("SELECT metric,value FROM weights"))
                return {k: float(rows.get(k, value)) for k, value in DEFAULT_WEIGHTS.items()}
            if mode not in MODE_WEIGHTS:
                raise ValueError("mode must be war or cwl")
            rows = dict(conn.execute("SELECT metric,value FROM mode_weights WHERE mode=?", (mode,)))
        return {k: float(rows.get(k, v)) for k, v in MODE_WEIGHTS[mode].items()}

    def set_weight(self, metric: str, percent: float, mode: str | None = None) -> dict[str, float]:
        defaults = MODE_WEIGHTS.get(mode) if mode is not None else DEFAULT_WEIGHTS
        if defaults is None or metric not in defaults or not 0 <= percent <= 100:
            raise ValueError("Invalid mode/metric or weight; use 0..100")
        with sqlite3.connect(self.path, timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = self.weights(mode)
            others = [x for x in defaults if x != metric]
            old_total = sum(previous[m] for m in others)
            free = 100.0 - percent
            values = {metric: round(float(percent), 4)}
            for key in others:
                values[key] = round(free * previous[key] / old_total if old_total
                                    else free / len(others), 4)
            values[others[-1]] = round(100.0 - sum(v for k, v in values.items()
                                                   if k != others[-1]), 4)
            if mode:
                conn.executemany("UPDATE mode_weights SET value=? WHERE mode=? AND metric=?",
                                 [(value, mode, key) for key, value in values.items()])
            else:
                conn.executemany("UPDATE weights SET value=? WHERE metric=?",
                                 [(value, key) for key, value in values.items()])
            conn.commit()
        return self.weights(mode)

    def get_preferences(self, clan_tag: str) -> dict[str, dict]:
        with sqlite3.connect(self.path, timeout=10) as conn:
            rows = conn.execute("""SELECT player_tag,last_seen_manual,last_seen_reporter
                                   FROM preferences WHERE clan_tag=?""", (clan_tag,)).fetchall()
        return {tag: {"lastSeenManual": seen, "lastSeenReporter": reporter} for tag, seen, reporter in rows}

    def set_cwl(self, clan_tag: str, player_tag: str, preference: str) -> None:
        """Legacy CLI compatibility only. This opt-in is no longer used for selection."""
        if preference not in PREFS:
            raise ValueError("CWL preference must be in/out/unknown")
        with sqlite3.connect(self.path, timeout=10) as conn:
            conn.execute("""INSERT INTO preferences(clan_tag,player_tag,cwl_opt,updated)
                VALUES(?,?,?,?)
                ON CONFLICT(clan_tag,player_tag)
                DO UPDATE SET cwl_opt=excluded.cwl_opt,updated=excluded.updated""",
                         (clan_tag,player_tag,preference,utc_now().isoformat()))

    def report_last_seen(self, clan_tag: str, player_tag: str,
                         value: str | None, reported_by: int) -> None:
        stamp = parse_utc(value)
        if value is not None and stamp is None:
            raise ValueError("Expected timezone-aware timestamp")
        if stamp and stamp > utc_now() + timedelta(minutes=1):
            raise ValueError("Future last-seen timestamps not permitted")
        with sqlite3.connect(self.path, timeout=10) as conn:
            conn.execute("""INSERT INTO preferences(
                clan_tag,player_tag,cwl_opt,last_seen_manual,last_seen_reporter,updated
            ) VALUES(?,?,'unknown',?,?,?)
            ON CONFLICT(clan_tag,player_tag)
            DO UPDATE SET last_seen_manual=excluded.last_seen_manual,
                          last_seen_reporter=excluded.last_seen_reporter,
                          updated=excluded.updated""",
                         (clan_tag,player_tag,value,reported_by,utc_now().isoformat()))


def score_and_categorize(
    ranking: list[dict], weights: dict[str, float], prefs: dict[str, dict],
    mode: str = "war", now: datetime | None = None, size: int = 15,
) -> dict:
    """Independent scoring for KV and CWL. Participation and bans are HARD gates."""
    if mode not in MODE_WEIGHTS:
        raise ValueError("mode must be war or cwl")
    if mode == "cwl":
        size = 15
    elif size not in range(5, 51, 5):
        raise ValueError("war size must be 5..50 in steps of 5")
    now = now or utc_now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if set(weights) != set(MODE_WEIGHTS[mode]):
        # Older drafts/tests passed old weight keys. Retain analysis behavior,
        # but real runtime always passes mode-specific weights.
        weights = dict(MODE_WEIGHTS[mode])
    strength_data = relative_strength(ranking)
    max_th = max(18, max((int(p.get("townHallLevel") or 0) for p in ranking), default=18))
    max_donations = max((int(m.get("donations") or 0) for m in ranking), default=0)
    max_raid = max((int(m.get("raidLoot") or 0) for m in ranking), default=0)
    groups = {x: [] for x in ("main","bench","inactive","optout","needs_review",
                              "cwl_review","twinks","kick","promotion","bonus")}
    full = []
    for original in ranking:
        p = dict(original)
        tag = str(p.get("tag") or "")
        prefs_for_tag = prefs.get(tag) or {}
        war_history = p.get("history") or {}
        record = war_history if mode == "war" else (p.get("cwlHistory") or {})
        count = int(record.get("wars") or 0)
        strength = strength_data.get(tag, {"relativeStrength": 50., "strengthCoverage": 0,
                                           "townHallPeers": 1})
        attendance = record.get("reliabilityPct")
        if attendance is None and record.get("attacksPossible"):
            attendance = 100 * float(record.get("attacksUsed") or 0) / record["attacksPossible"]
        quality = record.get("qualityPct")
        if quality is None and record.get("averageWeightedStars") is not None:
            quality = 100 * float(record["averageWeightedStars"]) / 3
        # The small sample is smoothed toward a neutral prior; not false 0%.
        def cautious(value: float | None) -> float:
            return round((min(100., max(0., float(value))) * count + 50 * 3)
                         / (count + 3), 1) if value is not None else 50.0
        th = int(p.get("townHallLevel") or 0)
        th_strength = round(100 * (max(0, min(th, max_th)) / max_th) ** 3, 1) if th else 50.
        combat_strength = (round(.8 * th_strength + .2 * strength["relativeStrength"], 1)
                           if mode == "cwl" else strength["relativeStrength"])
        components = {
            "strength": combat_strength,
            "discipline": cautious(attendance),
            "quality": cautious(quality),
            "role": ROLE_SCORE.get(p.get("role"), 50.0),
        }
        score = round(sum(weights[key] * components[key] for key in weights) / 100, 1)
        manual_dt = parse_utc(prefs_for_tag.get("lastSeenManual"))
        hours_idle = max(0., (now - manual_dt).total_seconds() / 3600) if manual_dt else None
        preference = p.get("warPreference", "unknown")
        available = "candidate"
        warnings = []
        if preference == "out":
            available = "optout"
            warnings.append("Красная отметка в игре: не включать ни в КВ, ни в ЛВК")
        elif hours_idle is not None and hours_idle > 48:
            available = "inactive"
            warnings.append("Вручную отмечено более 48 часов без входа")
        elif preference not in ("in", "out"):
            available = "needs_review"
            warnings.append("Неизвестно согласие из профиля CoC")
        if mode == "cwl" and p.get("missedThisCWL"):
            warnings.append("Пропущен раунд текущей ЛВК: уточнить причину и готовность; решение за руководством")
        if hours_idle is None:
            warnings.append("Последний вход недоступен через официальный API")
        if count < 3:
            warnings.append("Недостаточно наблюдаемых завершённых войн для устойчивой оценки")
        streak = int(p.get("missedWarsInARow") or 0)
        promotion = promotion_score(p, max_donations, max_raid)
        p.update({
            "selectionScore": score, "scoreWeights": dict(weights),
            "scoreComponents": components, "relativeStrength": strength["relativeStrength"],
            "townHallStrength": th_strength, "combatStrength": combat_strength,
            "strengthCoverage": strength["strengthCoverage"],
            "townHallPeers": strength["townHallPeers"],
            "confidence": "high" if count >= 5 and strength["strengthCoverage"] > 0 else
                          "medium" if count >= 3 else "low",
            "cwlPreference": preference, "lastSeenManual": prefs_for_tag.get("lastSeenManual"),
            "lastSeenHoursAgo": round(hours_idle, 1) if hours_idle is not None else None,
            "lastSeenSource": "reported_by_leadership" if manual_dt else "unavailable",
            "eligibility": available, "reasons": warnings,
            "missedWarsInARow": streak, "contributionScore": promotion,
        })
        # 3 completely missed assigned wars in a row: review for kick.
        # One missing attack out of two is bad discipline but not a missed war.
        if streak >= 3:
            p["reasons"].append("Три назначенные войны подряд без единой атаки: рассмотрение кика")
            groups["kick"].append(p)
        elif (available == "inactive" and hours_idle is not None and hours_idle >= 168
              and int(war_history.get("missedAttacks") or 0) >= 3 and
              int(war_history.get("wars") or 0) >= 4):
            groups["kick"].append(p)
        if (p.get("role") not in {"coLeader","leader"} and
            (int(war_history.get("wars") or 0) + int((p.get("cwlHistory") or {}).get("wars") or 0)) >= 3 and
            promotion["score"] is not None and promotion["score"] >= 75 and
            promotion["coverage"] >= 50 and available != "inactive" and streak == 0):
            groups["promotion"].append(p)
        cwl = p.get("cwlHistory") or {}
        if (int(cwl.get("wars") or 0) >= 3
            and int(cwl.get("missedAttacks") or 0) == 0
            and cwl.get("qualityPct") is not None
            and float(cwl["qualityPct"]) >= 75):
            groups["bonus"].append(p)
        if p.get("ownerAccountCount", 0) > 1:
            groups["twinks"].append(p)
        if mode == "cwl" and p.get("missedThisCWL"):
            groups["cwl_review"].append(p)
        full.append(p)
    eligible = sorted((p for p in full if p["eligibility"] == "candidate"),
                      key=lambda p: (-p["selectionScore"], -int(p.get("townHallLevel") or 0),
                                     str(p.get("name") or "")))
    groups["main"] = eligible[:max(0, int(size))]
    groups["bench"] = eligible[max(0, int(size)):]
    for p in full:
        if p["eligibility"] in ("inactive", "optout", "needs_review"):
            groups[p["eligibility"]].append(p)
    groups["kick"].sort(key=lambda p: (p["selectionScore"], p.get("name") or ""))
    groups["promotion"].sort(key=lambda p: -(p["contributionScore"].get("score") or 0))
    groups["bonus"].sort(key=lambda p: -(p.get("cwlHistory", {}).get("qualityPct") or 0))
    rating = sorted(full, key=lambda p: (-p["selectionScore"], -int(p.get("townHallLevel") or 0),
                                          str(p.get("tag") or "")))
    for place, p in enumerate(rating, 1):
        p["clanRank"] = place
    return {
        "clanRanking": rating, "lineupSize": size,
        "mode": mode, "weights": dict(weights), "categories": groups,
        "recommendedLineup": full, "recommendedSize": min(size, len(groups["main"])),
        "allPlayers": full,
        "selectionNote": "Согласие CoC — обязательное условие для КВ и ЛВК. "
                         "Последний вход только из ручной отметки; автоматических киков нет.",
    }
