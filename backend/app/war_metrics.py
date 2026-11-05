"""Pure war analysis. No unsupported Clash API fields or automatic moderation."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

LEAGUE_ORDER = {
    "unranked": 0, "bronze": 1, "silver": 2, "gold": 3,
    "crystal": 4, "master": 5, "champion": 6,
    "titan": 7, "legend": 8,
}


def _league_index(name: str) -> float | None:
    lower = (name or "").lower()
    # Current ranked leagues carry a global tier number (e.g. Electro League 32).
    if "legend" in lower:
        tier = re.search(r"\b(iii|ii|i)\b", lower)
        return {"iii": 34., "ii": 35., "i": 36.}.get(tier.group(1), 34.) if tier else 34.
    numeric_tier = re.search(r"\b(\d+)\s*$", lower)
    if numeric_tier:
        return float(numeric_tier.group(1))
    for league, level in LEAGUE_ORDER.items():
        if league in lower:
            tier = re.search(r"\b(i{1,3}|iv|v)\b", lower)
            bonus = {"iii": .0, "ii": .3, "i": .6}.get(tier.group(1), .3) if tier else .3
            return level + bonus
    return None


def _peer_percentile(value: float | None, cohort: list[float]) -> float | None:
    if value is None or len(cohort) < 2:
        return None
    # Equal players share 50, not 100. Ranks refer to same-TH peers only.
    return round(100 * (sum(v < value for v in cohort) +
                        .5 * sum(v == value for v in cohort)) / len(cohort), 1)


def relative_strength(players: list[dict[str, Any]]) -> dict[str, dict]:
    """TH-matched experience/hero/equipment/league percentiles within this clan.

    The API provides no building levels. One-person TH cohorts have no defensible
    relative ranking: keep 50 neutral and mark the source/coverage.
    """
    cohorts: dict[int, list[dict]] = defaultdict(list)
    for member in players:
        if member.get("tag"):
            cohorts[int(member.get("townHallLevel") or 0)].append(member)
    out = {}
    for members in cohorts.values():
        experience = [float(p["expLevel"]) for p in members if isinstance(p.get("expLevel"), (int, float))]
        heroes = [float(p["heroesLevel"]) for p in members if isinstance(p.get("heroesLevel"), (int, float))]
        equipment = [float(p["heroEquipmentScore"]) for p in members if isinstance(p.get("heroEquipmentScore"), (int, float))]
        leagues = [x for p in members if (x := _league_index(str(p.get("league") or ""))) is not None]
        for player in members:
            pairs = (
                (15, _peer_percentile(float(player["expLevel"]), experience)
                 if isinstance(player.get("expLevel"), (int, float)) else None),
                (55, _peer_percentile(float(player["heroesLevel"]), heroes)
                 if isinstance(player.get("heroesLevel"), (int, float)) else None),
                (20, _peer_percentile(float(player["heroEquipmentScore"]), equipment)
                 if isinstance(player.get("heroEquipmentScore"), (int, float)) else None),
                (10, _peer_percentile(_league_index(str(player.get("league") or "")), leagues)),
            )
            known = [(weight, val) for weight, val in pairs if val is not None]
            score = round(sum(weight * value for weight, value in known) / sum(weight for weight, _ in known), 1) if known else 50.0
            out[str(player["tag"])] = {
                "relativeStrength": score,
                "strengthCoverage": sum(weight for weight, _ in known),
                "townHallPeers": len(members),
                "method": "TH-matched clan peers; 15% experience, 55% heroes, 20% equipment, 10% league",
            }
    return out


def missed_war_streak(wars: list[dict], clan_tag: str) -> dict[str, int]:
    """Consecutive FULLY missed wars among wars actually featuring each player."""
    seen: set[str] = set()
    streaks: dict[str, int] = {}
    for war in sorted(wars, key=lambda x: x.get("startTime") or "", reverse=True):
        if war.get("state") != "warEnded":
            continue
        ours = next((side for side in (war.get("clan") or {}, war.get("opponent") or {})
                     if side.get("tag") == clan_tag), None)
        if not ours:
            continue
        for member in ours.get("members") or []:
            tag = member.get("tag")
            if not tag or tag in seen:
                continue
            if member.get("attacks"):
                seen.add(tag)
            else:
                streaks[tag] = streaks.get(tag, 0) + 1
    return streaks


def missed_current_cwl(wars: list[dict], clan_tag: str) -> set[str]:
    """In the current CWL group, a single FULLY missed round bars later rounds."""
    missed: set[str] = set()
    for war in wars:
        if war.get("state") != "warEnded":
            continue
        ours = next((side for side in (war.get("clan") or {}, war.get("opponent") or {})
                     if side.get("tag") == clan_tag), None)
        if ours:
            missed.update(m["tag"] for m in (ours.get("members") or [])
                          if m.get("tag") and not m.get("attacks"))
    return missed


def promotion_score(player: dict, donation_max: int, raid_max: int) -> dict:
    """40% war, 25% current-season donations, 20% last raid, 15% attendance.

    Ignore entirely missing metrics and show coverage. Never pretend the public
    API exposes per-player Clan Games points.
    """
    # Consistent measurement period: use observed completed attacks from both
    # regular war and CWL. These remain separate for mode-specific lineup scores.
    regular = player.get("history") or {}
    cwl = player.get("cwlHistory") or {}
    observed_wars = int(regular.get("wars") or 0) + int(cwl.get("wars") or 0)
    observed_used = int(regular.get("attacksUsed") or 0) + int(cwl.get("attacksUsed") or 0)
    observed_possible = int(regular.get("attacksPossible") or 0) + int(cwl.get("attacksPossible") or 0)
    parts = []
    for group in (regular, cwl):
        used = int(group.get("attacksUsed") or 0)
        quality = group.get("qualityPct")
        if quality is None and group.get("averageWeightedStars") is not None:
            quality = min(100., float(group["averageWeightedStars"]) / 3 * 100)
        if used and quality is not None:
            parts.append((used, float(quality)))
    quality = (sum(used * value for used, value in parts) / sum(used for used, _ in parts)
               if parts and observed_wars >= 3 else None)
    participation = (100 * observed_used / observed_possible
                     if observed_possible and observed_wars >= 3 else None)
    donations = player.get("donations")
    raids = player.get("raidLoot")
    components = {
        "war": (40, quality),
        "donations": (25, min(100., 100 * donations / donation_max) if donations is not None and donation_max > 0 else None),
        "raids": (20, min(100., 100 * raids / raid_max) if raids is not None and raid_max > 0 else None),
        "discipline": (15, participation),
    }
    known = [(w, float(v)) for w, v in components.values() if v is not None]
    return {
        "score": round(sum(w * v for w, v in known) / sum(w for w, _ in known), 1) if known else None,
        "coverage": sum(w for w, _ in known),
        "donationSeason": donations,
        "latestRaidLoot": raids,
        "clanGames": None,
        "warQuality": round(quality, 1) if quality is not None else None,
        "observedWars": observed_wars,
    }
