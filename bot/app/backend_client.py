from __future__ import annotations

from typing import Any

import httpx

from app.settings import settings


def build_url(path: str) -> str:
    return f"{settings.backend_url}{path}"


async def fetch_json(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(build_url(path))
    response.raise_for_status()
    return response.json()
