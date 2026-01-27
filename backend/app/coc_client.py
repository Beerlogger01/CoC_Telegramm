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
