import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from redis.asyncio import Redis

from app.coc_client import (
    ForbiddenError,
    InvalidTagError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    get_clan,
    get_player,
    get_war,
)
from app.settings import settings, validate_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    missing = validate_settings()
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        raise SystemExit(1)
    logger.info("Environment validation passed")

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    headers = {"Authorization": f"Bearer {settings.coc_token}"}
    client = httpx.AsyncClient(headers=headers, timeout=timeout)

    app.state.redis = redis
    app.state.http_client = client
    logger.info("Backend startup complete")
    try:
        yield
    finally:
        await client.aclose()
        await redis.close()
        logger.info("Backend shutdown complete")


app = FastAPI(lifespan=lifespan)


def get_redis(request: Request) -> Redis:
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


@app.get("/health")
async def health():
    return {"status": "ok"}
