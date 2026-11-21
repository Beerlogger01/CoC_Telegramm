"""Telegram member-tag integration (Bot API 9.5) for linked CoC players."""
from __future__ import annotations

import asyncio
import logging

import httpx
from app.ui import safe_tag_name

logger = logging.getLogger(__name__)


async def assign_game_tag(context, group_id: int, user_id: int, name: str) -> bool:
    """Assign a regular member's nickname; skip administrators and absent users."""
    label = safe_tag_name(name)
    if not label:
        return False
    try:
        member = await context.bot.get_chat_member(chat_id=group_id, user_id=user_id)
        if member.status == "administrator":
            if getattr(member, "can_be_edited", False):
                await context.bot.set_chat_administrator_custom_title(group_id, user_id, label)
                return True
            return False
        if member.status in {"creator", "left", "kicked", "banned"}:
            return False
        if member.status == "restricted" and not getattr(member, "is_member", False):
            return False
        # PTB 21.4 predates Telegram Bot API 9.5; call the official endpoint directly.
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{context.bot.token}/setChatMemberTag",
                json={"chat_id": group_id, "user_id": user_id, "tag": label},
            )
        if response.status_code == 200 and response.json().get("ok") is True:
            return True
        logger.warning("Member tag rejected by Telegram status=%s user_id=%s",
                       response.status_code, user_id)
    except Exception:
        # Never print request/exception URL; Telegram embeds the bot token in it.
        logger.warning("Member tag could not be updated user_id=%s", user_id)
    return False


async def sync_tags_daily(context) -> None:
    """Refresh nicknames and roles; edit admin titles only when Telegram permits."""
    from app.backend_client import fetch_json
    from app.access_policy import highest_role
    from app.settings import settings
    storage = context.application.bot_data["storage"]
    group_id = settings.clan_group_id
    if group_id is None:
        return
    bindings = storage.get_bindings_for_group(group_id)
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            roster = await fetch_json(client, "/clan?fresh=true")
        members = roster.get("memberList", [])
        by_tag = {m["tag"]: m for m in members}
    except Exception:
        logger.warning("Role refresh unavailable; current Telegram labels preserved")
        return
    semaphore = asyncio.Semaphore(3)
    async def one(binding):
        async with semaphore:
            try:
                accounts = storage.get_user_accounts(group_id, binding.telegram_user_id)
                tags = [a.coc_player_tag for a in accounts]
                role = highest_role(tags, members)
                player = next((by_tag[t] for t in tags if t in by_tag), None)
                if not player:
                    return
                titles = {"leader": "Глава", "coLeader": "Со-глава", "admin": "Старейш.", "member": "Участник"}
                # Reserve space for both nickname and actual highest clan role.
                title = titles.get(role, "")
                nickname = player.get("name", "")[:max(1, 15-len(title))]
                await assign_game_tag(context, group_id, binding.telegram_user_id, nickname + "·" + title)
            except Exception:
                logger.warning("Tag refresh unavailable for member=%s", binding.telegram_user_id)
    await asyncio.gather(*(one(binding) for binding in bindings))
