from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from redis.asyncio import Redis

from app.cache import get_cached_json, set_cached_json
from app.settings import settings

logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r"^[0289PYLQGRJCUV]+$")


class InvalidTagError(ValueError):
    pass


class NotFoundError(ValueError):
    pass


class UnauthorizedError(ValueError):
    pass


class ForbiddenError(ValueError):
    pass


class RateLimitError(ValueError):
    pass


def normalize_tag(tag: str) -> str:
    cleaned = tag.replace(" ", "").strip().upper()
    if not cleaned.startswith("#"):
        cleaned = f"#{cleaned}"
    raw = cleaned.lstrip("#")
    if not raw or not TAG_PATTERN.fullmatch(raw):
        logger.warning("Invalid tag format input=%s normalized=%s", tag, cleaned)
        raise InvalidTagError("Invalid tag format")
    logger.info("Normalized tag input=%s normalized=%s", tag, cleaned)
    return cleaned


def encode_tag(tag: str) -> str:
    return normalize_tag(tag).replace("#", "%23")


async def fetch_with_cache(
    client: httpx.AsyncClient,
    redis: Redis,
    cache_key: str,
    url: str,
) -> dict[str, Any]:
    cached = await get_cached_json(redis, cache_key)
    if cached:
        logger.info("Cache hit key=%s", cache_key)
        return cached

    try:
        logger.info("CoC API request url=%s", url)
        response = await client.get(url)
    except httpx.TimeoutException as exc:
        logger.warning("CoC API timeout", exc_info=exc)
        raise TimeoutError("CoC API timeout") from exc
    except httpx.RequestError as exc:
        logger.warning("CoC API request failed", exc_info=exc)
        raise RuntimeError("CoC API unavailable") from exc

    logger.info("CoC API response status=%s url=%s", response.status_code, url)
    if response.status_code == 401:
        raise UnauthorizedError("Unauthorized token")
    if response.status_code == 403:
        raise ForbiddenError("Forbidden (IP not whitelisted or token invalid)")
    if response.status_code == 429:
        raise RateLimitError("Rate limit exceeded")
    if response.status_code == 404:
        raise NotFoundError("Not found")
    if response.status_code >= 400:
        raise RuntimeError("CoC API error")

    payload = response.json()
    await set_cached_json(redis, cache_key, payload, settings.cache_ttl_seconds)
    return payload


async def get_clan(client: httpx.AsyncClient, redis: Redis) -> dict[str, Any]:
    clan_tag = normalize_tag(settings.coc_clan_tag)
    cache_key = f"clan:{clan_tag}"
    url = f"{settings.coc_api_base}/clans/{encode_tag(clan_tag)}"
    return await fetch_with_cache(client, redis, cache_key, url)


async def get_player(client: httpx.AsyncClient, redis: Redis, tag: str) -> dict[str, Any]:
    normalized = normalize_tag(tag)
    cache_key = f"player:{normalized}"
    url = f"{settings.coc_api_base}/players/{encode_tag(normalized)}"
    return await fetch_with_cache(client, redis, cache_key, url)


async def get_war(client: httpx.AsyncClient, redis: Redis) -> dict[str, Any]:
    clan_tag = normalize_tag(settings.coc_clan_tag)
    cache_key = f"war:{clan_tag}"
    url = f"{settings.coc_api_base}/clans/{encode_tag(clan_tag)}/currentwar"
    return await fetch_with_cache(client, redis, cache_key, url)


async def get_clan_members(client: httpx.AsyncClient, redis: Redis, limit: int = 50) -> dict[str, Any]:
    """Get clan members, sorted by trophies."""
    clan_data = await get_clan(client, redis)
    members = clan_data.get("memberList", [])
    # Sort by trophies descending
    sorted_members = sorted(members, key=lambda m: m.get("trophies", 0), reverse=True)
    return {
        "clanName": clan_data.get("name"),
        "clanTag": clan_data.get("tag"),
        "members": sorted_members[:limit]
    }


async def get_clan_activity_report(client: httpx.AsyncClient, redis: Redis) -> dict[str, Any]:
    """Get comprehensive clan activity report."""
    clan_tag = normalize_tag(settings.coc_clan_tag)
    
    # Get clan data
    clan_data = await get_clan(client, redis)
    members = clan_data.get("memberList", [])
    
    # Get war data
    war_data = await get_war(client, redis)
    war_state = war_data.get("state", "notInWar")
    
    # Calculate stats
    total_members = len(members)
    total_trophies = sum(m.get("trophies", 0) for m in members)
    avg_trophies = total_trophies // total_members if total_members > 0 else 0
    
    # Get war stars if in war
    war_stars = 0
    if war_state == "inWar":
        war_members = war_data.get("clan", {}).get("members", [])
        war_stars = sum(m.get("stars", 0) for m in war_members)
    
    # Get members sorted by last seen (activity)
    members_by_activity = sorted(
        members, 
        key=lambda m: m.get("lastSeen", "2000-01-01T00:00:00.000Z"),
        reverse=True
    )
    
    # Most and least active
    most_active = members_by_activity[:5] if members_by_activity else []
    least_active = members_by_activity[-5:] if len(members_by_activity) > 5 else []
    
    # War attacks info
    war_attacks_done = 0
    war_attacks_remaining = 0
    if war_state == "inWar":
        war_members = war_data.get("clan", {}).get("members", [])
        war_attacks_done = sum(1 for m in war_members if m.get("attacks") and len(m.get("attacks", [])) > 0)
        war_attacks_remaining = len(war_members) - war_attacks_done
    
    return {
        "clanName": clan_data.get("name"),
        "clanTag": clan_data.get("tag"),
        "clanLevel": clan_data.get("clanLevel"),
        "members": {
            "total": total_members,
            "totalTrophies": total_trophies,
            "avgTrophies": avg_trophies,
        },
        "war": {
            "state": war_state,
            "stars": war_stars,
            "attacksDone": war_attacks_done,
            "attacksRemaining": war_attacks_remaining,
            "enemyName": war_data.get("opponent", {}).get("name") if war_state == "inWar" else None,
        },
        "activity": {
            "mostActive": [
                {
                    "name": m.get("name"),
                    "tag": m.get("tag"),
                    "role": m.get("role"),
                    "lastSeen": m.get("lastSeen"),
                    "trophies": m.get("trophies"),
                }
                for m in most_active
            ],
            "leastActive": [
                {
                    "name": m.get("name"),
                    "tag": m.get("tag"),
                    "role": m.get("role"),
                    "lastSeen": m.get("lastSeen"),
                    "trophies": m.get("trophies"),
                }
                for m in least_active
            ],
        }
    }

async def get_clan_raids(client: httpx.AsyncClient, redis: Redis) -> dict[str, Any]:
    """Get clan raids (capital games) information."""
    clan_tag = normalize_tag(settings.coc_clan_tag)
    cache_key = f"raids:{clan_tag}"
    url = f"{settings.coc_api_base}/clans/{encode_tag(clan_tag)}/capitalraidseasons"
    
    try:
        data = await fetch_with_cache(client, redis, cache_key, url)
        
        # Get current raid if available
        items = data.get("items", [])
        if not items:
            return {"currentRaid": None}
        
        # Most recent raid is first
        current = items[0]
        state = current.get("state", "unknown")
        
        # Parse dates
        start_time = current.get("startTime", "N/A")
        end_time = current.get("endTime", "N/A")
        
        raid_info = {
            "state": state,
            "capitalName": "Capital Raid",
            "startTime": start_time,
            "endTime": end_time,
        }
        
        # If ongoing, include resource details
        if state == "ongoing":
            clan_capital = current.get("clan", {})
            resources = clan_capital.get("resources", [])
            raid_info["clan"] = {"resources": resources}
        
        return {"currentRaid": raid_info}
    except Exception:
        logger.exception("Failed to get clan raids")
        return {"currentRaid": None}

async def get_clan_games(client: httpx.AsyncClient, redis: Redis) -> dict[str, Any]:
    """Get clan games information."""
    clan_tag = normalize_tag(settings.coc_clan_tag)
    cache_key = f"games:{clan_tag}"
    url = f"{settings.coc_api_base}/clans/{encode_tag(clan_tag)}"
    
    try:
        data = await fetch_with_cache(client, redis, cache_key, url)
        
        # Get clan games info
        clan_games = data.get("clanGames", {})
        
        if not clan_games:
            return {"currentGames": None}
        
        state = clan_games.get("state", "notInProgress")
        start_time = clan_games.get("startTime", "N/A")
        end_time = clan_games.get("endTime", "N/A")
        
        games_info = {
            "state": state,
            "startTime": start_time,
            "endTime": end_time,
        }
        
        # If in progress, include score details
        if state == "inProgress":
            games_info["score"] = clan_games.get("memberGameInfo", {}).get("totalScore", "N/A")
        
        return {"currentGames": games_info}
    except Exception:
        logger.exception("Failed to get clan games")
        return {"currentGames": None}


async def get_player_activity(client: httpx.AsyncClient, redis: Redis) -> dict[str, Any]:
    """Get clan members activity statistics."""
    clan_data = await get_clan(client, redis)
    members = clan_data.get("memberList", [])
    war_data = await get_war(client, redis)
    war_state = war_data.get("state", "notInWar")
    war_members = war_data.get("clan", {}).get("members", []) if war_state == "inWar" else []
    
    # Create activity scores for each member
    member_activity = []
    
    for member in members:
        tag = member.get("tag")
        name = member.get("name", "Unknown")
        
        # Donation score
        donations = member.get("donations", 0)
        
        # Last seen - convert ISO timestamp to days ago
        last_seen = member.get("lastSeen", "2000-01-01T00:00:00.000Z")
        
        # War attacks
        war_attacks = 0
        if war_state == "inWar":
            war_member = next((m for m in war_members if m.get("tag") == tag), None)
            if war_member and war_member.get("attacks"):
                war_attacks = len(war_member.get("attacks", []))
        
        # Combined activity score (higher = more active)
        activity_score = donations + (war_attacks * 10)
        
        member_activity.append({
            "name": name,
            "tag": tag,
            "donations": donations,
            "lastSeen": last_seen,
            "warAttacks": war_attacks,
            "activityScore": activity_score,
            "townHallLevel": member.get("townHallLevel", 0),
            "trophies": member.get("trophies", 0),
        })
    
    # Sort by activity score
    most_active = sorted(member_activity, key=lambda x: x["activityScore"], reverse=True)[:10]
    least_active = sorted(member_activity, key=lambda x: x["activityScore"])[:10]
    
    return {
        "mostActive": most_active,
        "leastActive": least_active,
    }


async def get_next_war_analysis(client: httpx.AsyncClient, redis: Redis) -> dict[str, Any]:
    """Analyze and rank members for next war based on:
    - War stars from last war
    - Player experience level
    - War league and season
    - Hero equipment quality
    - Trophies and town hall level
    """
    clan_data = await get_clan(client, redis)
    members = clan_data.get("memberList", [])
    
    war_data = await get_war(client, redis)
    war_state = war_data.get("state", "notInWar")
    
    # Get war log to analyze last war performance
    clan_tag = normalize_tag(settings.coc_clan_tag)
    cache_key = f"warlog:{clan_tag}"
    url = f"{settings.coc_api_base}/clans/{encode_tag(clan_tag)}/warlog"
    warlog_data = await fetch_with_cache(client, redis, cache_key, url)
    warlog_items = warlog_data.get("items", []) if warlog_data else []
    last_war = warlog_items[0] if warlog_items else {}
    
    # Get clan war league data
    cwl_cache_key = f"cwl:{clan_tag}"
    cwl_url = f"{settings.coc_api_base}/clans/{encode_tag(clan_tag)}/currentwarleaguegroup"
    try:
        cwl_data = await fetch_with_cache(client, redis, cwl_cache_key, cwl_url)
    except (NotFoundError, ForbiddenError):
        cwl_data = {"state": "unknown"}
    
    member_rankings = []
    
    for member in members:
        tag = member.get("tag")
        name = member.get("name", "Unknown")
        
        # Basic stats
        town_hall = member.get("townHallLevel", 0)
        trophies = member.get("trophies", 0)
        exp_level = member.get("expLevel", 0)
        war_stars = member.get("warStars", 0)
        donations = member.get("donations", 0)
        war_preference = member.get("warPreference", "out")
        
        # League info
        league = member.get("league", {})
        league_name = league.get("name", "Unranked")
        league_id = league.get("id", 0)
        
        # Get full player data for hero equipment
        try:
            player_data = await get_player(client, redis, tag)
        except Exception:
            player_data = {}
        
        # Hero equipment quality score
        equipment = player_data.get("heroEquipment", [])
        equipment_score = sum(e.get("level", 0) for e in equipment)
        equipment_count = len(equipment)
        
        # Heroes level sum
        heroes = player_data.get("heroes", [])
        heroes_level = sum(h.get("level", 0) for h in heroes)
        
        # Last war performance (from warlog)
        last_war_stars = 0
        last_war_destruction = 0
        if last_war:
            clan_members = last_war.get("clan", {}).get("members", [])
            for wm in clan_members:
                if wm.get("tag") == tag:
                    last_war_stars = wm.get("stars", 0)
                    attacks = wm.get("attacks", [])
                    if attacks:
                        last_war_destruction = attacks[0].get("destructionPercentage", 0)
        
        # Combat readiness score (higher = better)
        # Components:
        # - Heroes level (max ~700 for all heroes)
        # - Equipment level (varies)
        # - War preference (in = +50, out = -50)
        # - Experience level (max ~256)
        # - Trophies (relevance lower)
        combat_score = (
            heroes_level * 2 +  # Double weight on heroes
            equipment_score +    # Equipment level sum
            exp_level +          # Experience level
            (50 if war_preference == "in" else -50) +
            (trophies // 100)    # Normalize trophies
        )
        
        # War readiness score combines all factors
        war_readiness = (
            (last_war_stars * 50) +          # Last war performance
            (last_war_destruction) +         # Destruction %
            combat_score +                   # Combat readiness
            (war_stars / 10) +               # Historical war stars
            (league_id / 1000)               # League level
        )
        
        member_rankings.append({
            "name": name,
            "tag": tag,
            "townHallLevel": town_hall,
            "trophies": trophies,
            "expLevel": exp_level,
            "warStars": war_stars,
            "donations": donations,
            "warPreference": war_preference,
            "league": league_name,
            "leagueId": league_id,
            "heroesLevel": heroes_level,
            "heroEquipmentScore": equipment_score,
            "heroEquipmentCount": equipment_count,
            "lastWarStars": last_war_stars,
            "lastWarDestruction": last_war_destruction,
            "combatReadiness": combat_score,
            "warReadiness": war_readiness,
        })
    
    # Sort by war readiness (descending)
    ranked = sorted(member_rankings, key=lambda x: x["warReadiness"], reverse=True)
    
    return {
        "clanName": clan_data.get("name"),
        "clanTag": clan_data.get("tag"),
        "cwlState": cwl_data.get("state"),
        "currentWarState": war_state,
        "recommendedLineup": ranked,
        "topTen": ranked[:10],
        "analysisFactors": {
            "lastWarPerformance": "Stars and destruction % from most recent war",
            "combatReadiness": "Heroes level, equipment, experience, war preference",
            "warReadiness": "Combined score for next war prediction",
            "sortedBy": "warReadiness (descending - best first)",
        }
    }
