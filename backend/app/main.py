import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from app.cache import TTLCache

from app.coc_client import (
    ForbiddenError,
    InvalidTagError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    get_clan,
    get_player,
    get_war,
    get_regular_war,
    get_league_wars,
    get_clan_members,
    get_clan_activity_report,
    get_clan_raids,
    get_clan_games,
    get_player_activity,
    get_next_war_analysis,
    get_cwl_summary,
    normalize_tag,
    encode_tag,
)
from app.settings import settings, validate_settings
from app.war_history import WarHistory, completed_for_clan
from app.hall import hall_of_fame
from app.progress import ProgressStore
from app.selection import SelectionStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def collect_war_history(
    client: httpx.AsyncClient, cache: TTLCache, history: WarHistory
) -> None:
    """CWL collection must continue even if the ordinary-war endpoint fails."""
    while True:
        try:
            regular = await get_regular_war(client, cache)
            if history.save({**regular, "_warKind": "war"}):
                logger.info("Saved regular clan war snapshot")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Regular war unavailable; still collecting CWL")
        try:
            _, cwl_wars = await get_league_wars(client, cache)
            for war in cwl_wars:
                history.save({**war, "_warKind": "cwl"})
            if cwl_wars:
                logger.info("Reviewed %s CWL wars", len(cwl_wars))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("CWL collection unavailable; will retry")
        await asyncio.sleep(300)


async def collect_player_progress(client: httpx.AsyncClient, cache: TTLCache, store: ProgressStore):
    """Snapshot all clan members at most once per day; retry transient failures."""
    await asyncio.sleep(30)
    while True:
        try:
            clan = await get_clan(client, cache)
            semaphore = asyncio.Semaphore(3)
            async def sample(member: dict):
                if not member.get("tag"):
                    return
                async with semaphore:
                    try:
                        profile = await get_player(client, cache, member["tag"])
                        store.record(profile)
                    except Exception:
                        logger.warning("Could not collect one player snapshot")
            await asyncio.gather(*(sample(m) for m in clan.get("memberList", [])))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Player progress snapshot unavailable; will retry")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    missing = validate_settings()
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        raise SystemExit(1)
    logger.info("Environment validation passed")

    redis = TTLCache()
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    headers = {"Authorization": "Bearer " + str(settings.coc_token)}
    client = httpx.AsyncClient(headers=headers, timeout=timeout)

    app.state.redis = redis
    app.state.http_client = client
    history_path = os.environ.get("WAR_HISTORY_DB_PATH", "/data/war_history.db")
    app.state.history = WarHistory(history_path)
    app.state.progress = ProgressStore(os.environ.get("PLAYER_PROGRESS_DB_PATH", "/data/player_progress.db"))
    app.state.selection = SelectionStore(os.environ.get("SELECTION_DB_PATH", "/data/selection.db"))
    poller = asyncio.create_task(
        collect_war_history(client, redis, app.state.history),
        name="war-history-poller",
    )
    progress_poller = asyncio.create_task(
        collect_player_progress(client, redis, app.state.progress), name="player-progress-poller"
    )
    logger.info("Backend startup complete; war history DB=%s", history_path)
    try:
        yield
    finally:
        poller.cancel()
        progress_poller.cancel()
        try:
            await progress_poller
        except asyncio.CancelledError:
            pass
        try:
            await poller
        except asyncio.CancelledError:
            pass
        await client.aclose()
        await redis.close()
        logger.info("Backend shutdown complete")


app = FastAPI(lifespan=lifespan)


def get_redis(request: Request) -> TTLCache:
    return request.app.state.redis


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


@app.get("/clan")
async def clan(request: Request):
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_clan(client, redis)
    except InvalidTagError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/player/{tag}")
async def player(tag: str, request: Request):
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_player(client, redis, tag)
    except InvalidTagError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/war-results")
async def war_results(request: Request):
    """Stored, completed regular and CWL wars for one-time Telegram notifications."""
    clan_tag = normalize_tag(settings.coc_clan_tag)
    wars = request.app.state.history.completed(limit=50)
    return {"wars": completed_for_clan(wars, clan_tag)}


@app.get("/war")
async def war(request: Request):
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_war(client, redis)
    except InvalidTagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/top-players")
async def top_players(request: Request, limit: int = 10):
    """Get top clan members by trophies."""
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_clan_members(client, redis, limit=min(limit, 50))
    except InvalidTagError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/cwl")
async def cwl(request: Request):
    try:
        return await get_cwl_summary(get_http_client(request), get_redis(request))
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail="CWL token invalid") from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail="CWL access forbidden") from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail="CWL API rate limit") from exc
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="CWL API unavailable") from exc


@app.get("/progress/{tag}")
async def progress(tag: str, request: Request, days: int = 7):
    try:
        profile = await get_player(get_http_client(request), get_redis(request), tag)
        request.app.state.progress.record(profile)
        return {**request.app.state.progress.timeline(profile["tag"], days=days), "name": profile.get("name")}
    except InvalidTagError as exc:
        raise HTTPException(status_code=400, detail="Invalid player tag") from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Player not found") from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail="Invalid Clash API token") from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail="Clash API IP/token forbidden") from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail="Clash API rate limited") from exc
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Clash API unavailable") from exc


@app.get("/hall")
async def hall(request: Request):
    """Leaderboard by separate observable sources. No fictional games points."""
    client = get_http_client(request)
    cache = get_redis(request)
    clan = await get_clan(client, cache)
    try:
        raids = await get_clan_raids(client, cache)
    except (NotFoundError, ForbiddenError, RateLimitError, RuntimeError, TimeoutError):
        raids = {}
    return hall_of_fame(clan, request.app.state.history.completed(100),
                        raids.get("currentRaid"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/activity-report")
async def activity_report(request: Request):
    """Get clan activity report."""
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_clan_activity_report(client, redis)
    except InvalidTagError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/raids")
async def raids(request: Request):
    """Get clan raids (capital games) information."""
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_clan_raids(client, redis)
    except InvalidTagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/games")
async def games(request: Request):
    """Get clan games (Clan Games) information."""
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_clan_games(client, redis)
    except InvalidTagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/activity")
async def activity(request: Request):
    """Get clan members activity statistics."""
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await get_player_activity(client, redis)
    except InvalidTagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class PreferenceUpdate(BaseModel):
    preference: str


class WeightUpdate(BaseModel):
    metric: str
    value: float
    mode: str = "war"


class ManualLastSeenUpdate(BaseModel):
    last_seen_at: str | None = None
    reported_by: int


@app.get("/selection/weights")
async def selection_weights(request: Request, mode: str = "war"):
    try:
        return {"mode": mode, "weights": request.app.state.selection.weights(mode)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/selection/weights")
async def selection_weight_update(body: WeightUpdate, request: Request):
    try:
        return {"mode": body.mode, "weights": request.app.state.selection.set_weight(body.metric, body.value, mode=body.mode)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/selection/cwl/{tag}")
async def selection_cwl_preference(tag: str, body: PreferenceUpdate, request: Request):
    try:
        player_tag = normalize_tag(tag)
        request.app.state.selection.set_cwl(normalize_tag(settings.coc_clan_tag), player_tag,
                                            body.preference)
        return {"tag": player_tag, "preference": body.preference}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/selection/lastseen/{tag}")
async def selection_last_seen(tag: str, body: ManualLastSeenUpdate, request: Request):
    try:
        player_tag = normalize_tag(tag)
        request.app.state.selection.report_last_seen(
            normalize_tag(settings.coc_clan_tag), player_tag,
            body.last_seen_at, body.reported_by,
        )
        return {"tag": player_tag, "lastSeenSource": "reported_by_leadership",
                "lastSeenManual": body.last_seen_at}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/next-war")
async def next_war(request: Request, mode: str = "war", size: int | None = None):
    """Analyze and recommend lineup for next war."""
    if mode not in {"war", "cwl"}:
        raise HTTPException(status_code=400, detail="mode must be war or cwl")
    if size is not None and size not in range(5, 51, 5):
        raise HTTPException(status_code=400, detail="size must be 5..50 in steps of 5")
    redis = get_redis(request)
    client = get_http_client(request)
    try:
        return await redis.singleflight(f'analysis:{mode}:{size}', lambda: get_next_war_analysis(
            client, redis, request.app.state.history, request.app.state.selection, mode=mode, size=size))
    except InvalidTagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

class PlayerVerificationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


@app.post("/verify-player/{tag}")
async def verify_player(tag: str, body: PlayerVerificationRequest, request: Request):
    """Verify the in-game player API token without logging or retaining it."""
    try:
        player_tag = normalize_tag(tag)
    except InvalidTagError as exc:
        raise HTTPException(status_code=400, detail="Invalid tag") from exc
    client = get_http_client(request)
    url = f"{settings.coc_api_base}/players/{encode_tag(player_tag)}/verifytoken"
    try:
        response = await client.post(url, json={"token": body.token})
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="CoC verification timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="CoC verification unavailable") from exc
    if response.status_code in (401, 403, 429):
        raise HTTPException(status_code=response.status_code, detail="CoC rejected verification request")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="CoC verification error")
    return {"verified": response.json().get("status") == "ok"}
