"""Versioned development index, independent of war performance and behaviour."""
import math

SIEGE = {"Wall Wrecker", "Battle Blimp", "Stone Slammer", "Siege Barracks", "Log Launcher", "Flame Flinger", "Battle Drill", "Troop Launcher"}
WEIGHTS = {"heroes": 0.30, "troops": 0.30, "spells": 0.15, "siege": 0.10, "equipment": 0.15}


def component(items):
    if not isinstance(items, list):
        return None
    ratios = []
    for item in items:
        if not isinstance(item, dict) or item.get("village", "home") != "home":
            continue
        level, maximum = item.get("level"), item.get("maxLevel")
        if (type(level) in (int, float) and type(maximum) in (int, float)
                and math.isfinite(level) and math.isfinite(maximum) and maximum > 0 and 0 <= level <= maximum):
            ratios.append(level / maximum)
    return sum(ratios) / len(ratios) if ratios else None


def evaluate(profile):
    troops = profile.get("troops")
    army = [t for t in troops if t.get("name") not in SIEGE and not t.get("superTroopIsActive") and not str(t.get("name", "")).startswith("Super ")] if isinstance(troops, list) else None
    siege = [t for t in troops if t.get("name") in SIEGE] if isinstance(troops, list) else None
    groups = {"heroes": profile.get("heroes"), "troops": army, "spells": profile.get("spells"), "siege": siege, "equipment": profile.get("heroEquipment")}
    values = {k: component(v) for k, v in groups.items()}
    coverage = sum(WEIGHTS[k] for k, v in values.items() if v is not None)
    development = sum(WEIGHTS[k] * v for k, v in values.items() if v is not None) / coverage if coverage else None
    th = profile.get("townHallLevel")
    th = th if type(th) is int and th > 0 else None
    # Ten points per TH; development adds at most nine. TH bands never overlap.
    base = 10 * th if th else None
    contributions = {k: round(9 * WEIGHTS[k] * v / coverage, 3) if v is not None else None for k,v in values.items()}
    return {"tag": profile.get("tag"), "name": profile.get("name"), "townHallLevel": th,
            "accountStrength": round(base + 9 * development, 2) if base is not None and development is not None else None,
            "development": round(development * 100, 2) if development is not None else None,
            "coverage": round(coverage * 100), "components": values, "contributions": contributions,
            "thContribution": base, "formulaVersion": 1,
            "maximaScope": "API maxLevel; not guaranteed to be the maximum for this TH"}


def rank(profiles):
    rows = [evaluate(p) for p in profiles]
    for row in rows:
        peers = [r["development"] for r in rows if r["townHallLevel"] == row["townHallLevel"] and r["development"] is not None]
        value = row["development"]
        row["peerPercentile"] = round(100 * (sum(v < value for v in peers) + .5 * sum(v == value for v in peers)) / len(peers), 1) if value is not None and len(peers) > 1 else None
    return sorted(rows, key=lambda r: (r["accountStrength"] is None, -(r["accountStrength"] or 0), r["tag"] or ""))
