import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from app.backend_client import fetch_json
from app.bindings_storage import Binding, BindingsStorage
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def format_clan(payload: dict[str, Any]) -> str:
    return (
        f"*{payload.get('name', 'Clan')}*\n"
        f"Tag: `{payload.get('tag', 'N/A')}`\n"
        f"Level: {payload.get('clanLevel', 'N/A')}\n"
        f"Members: {payload.get('members', 'N/A')}\n"
        f"War League: {payload.get('warLeague', {}).get('name', 'N/A')}\n"
    )


def format_player(payload: dict[str, Any]) -> str:
    return (
        f"*{payload.get('name', 'Player')}*\n"
        f"Tag: `{payload.get('tag', 'N/A')}`\n"
        f"Town Hall: {payload.get('townHallLevel', 'N/A')}\n"
        f"Trophies: {payload.get('trophies', 'N/A')}\n"
        f"Best Trophies: {payload.get('bestTrophies', 'N/A')}\n"
        f"Clan: {payload.get('clan', {}).get('name', 'No clan')}\n"
    )


def format_war(payload: dict[str, Any]) -> str:
    return (
        f"*Current War*\n"
        f"State: {payload.get('state', 'N/A')}\n"
        f"Team Size: {payload.get('teamSize', 'N/A')}\n"
        f"Start: {payload.get('startTime', 'N/A')}\n"
        f"End: {payload.get('endTime', 'N/A')}\n"
    )


def normalize_tag(tag: str) -> str:
    cleaned = tag.replace(" ", "").upper()
    if not cleaned.startswith("#"):
        cleaned = f"#{cleaned}"
    return cleaned


def format_mention(user_id: int, name: str) -> str:
    safe_name = html.escape(name, quote=True)
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def parse_coc_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def attacks_used(member: dict[str, Any]) -> int:
    attacks = member.get("attacks")
    if isinstance(attacks, list):
        return len(attacks)
    if isinstance(attacks, int):
        return attacks
    return 0


def binding_error_message(status: int) -> str:
    if status == 400:
        return "Invalid player tag format."
    if status == 401:
        return "Backend token invalid. Please update the backend token."
    if status == 404:
        return "Player not found."
    if status == 429:
        return "Rate limit reached. Please try again later."
    if status == 503:
        return "Backend IP is not whitelisted for Clash of Clans."
    if status == 504:
        return "Backend timed out contacting Clash of Clans."
    return "Backend error while validating player."


def ensure_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Welcome! Use /clan, /player <tag>, or /war to get Clash of Clans info."
        )


async def clan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/clan")
            await update.message.reply_text(format_clan(payload), parse_mode=ParseMode.MARKDOWN)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 429:
                message = "Rate limit reached. Please try again later."
            elif status == 400:
                message = "Invalid clan tag configured."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching clan data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")


async def player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /player <tag>")
        return
    tag = normalize_tag(context.args[0])
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, f"/player/{tag.replace('#', '%23')}")
            await update.message.reply_text(format_player(payload), parse_mode=ParseMode.MARKDOWN)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 400:
                message = "Invalid player tag format."
            elif status == 404:
                message = "Player not found."
            elif status == 429:
                message = "Rate limit reached. Please try again later."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching player data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")


async def war(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/war")
            await update.message.reply_text(format_war(payload), parse_mode=ParseMode.MARKDOWN)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 429:
                message = "Rate limit reached. Please try again later."
            elif status == 400:
                message = "Invalid clan tag configured."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching war data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")


async def bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not ensure_group_chat(update):
        await update.message.reply_text("This command can only be used in group chats.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /bind #PLAYER_TAG")
        return
    tag = normalize_tag(context.args[0])
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            await fetch_json(client, f"/player/{tag.replace('#', '%23')}")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error validating player: %s", exc)
            await update.message.reply_text(binding_error_message(status))
            return
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable during bind: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")
            return
    storage: BindingsStorage = context.application.bot_data["storage"]
    now = datetime.now(timezone.utc).isoformat()
    binding = Binding(
        telegram_user_id=update.effective_user.id,
        group_id=update.effective_chat.id,
        coc_player_tag=tag,
        telegram_username=update.effective_user.username,
        telegram_full_name=update.effective_user.full_name,
        created_at=now,
    )
    storage.upsert_binding(binding)
    mention = format_mention(update.effective_user.id, update.effective_user.full_name)
    await update.message.reply_text(
        f"Bound {mention} to {html.escape(tag)}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def unbind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not ensure_group_chat(update):
        await update.message.reply_text("This command can only be used in group chats.")
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    removed = storage.delete_binding(update.effective_chat.id, update.effective_user.id)
    if removed:
        mention = format_mention(update.effective_user.id, update.effective_user.full_name)
        message = f"Removed binding for {mention}."
    else:
        message = "No binding found for your account in this group."
    await update.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def mytag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not ensure_group_chat(update):
        await update.message.reply_text("This command can only be used in group chats.")
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    binding = storage.get_binding(update.effective_chat.id, update.effective_user.id)
    if not binding:
        await update.message.reply_text("No tag bound for your account in this group.")
        return
    await update.message.reply_text(
        f"Your bound tag is {html.escape(binding.coc_player_tag)}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def war_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.war_reminder_enabled:
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/war")
        except httpx.HTTPStatusError as exc:
            logger.warning("Backend error fetching war data: %s", exc)
            return
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable for war reminder: %s", exc)
            return
    if payload.get("state") != "inWar":
        return
    end_time = parse_coc_time(payload.get("endTime"))
    if end_time is None:
        logger.warning("Could not parse war end time")
        return
    now = datetime.now(timezone.utc)
    time_to_end = end_time - now
    if time_to_end <= timedelta(0):
        return
    if time_to_end > timedelta(hours=settings.war_reminder_window_hours):
        return
    clan_members = payload.get("clan", {}).get("members", [])
    tags_missing_attacks = {
        normalize_tag(member.get("tag", ""))
        for member in clan_members
        if attacks_used(member) == 0 and member.get("tag")
    }
    if not tags_missing_attacks:
        return
    for group_id in storage.get_group_ids():
        bindings = storage.get_bindings_for_tags(group_id, tags_missing_attacks)
        if not bindings:
            continue
        user_ids = [binding.telegram_user_id for binding in bindings]
        cooldowns = storage.get_cooldowns(group_id, user_ids)
        mentions: list[str] = []
        reminded_user_ids: list[int] = []
        for binding in bindings:
            last_reminded = cooldowns.get(binding.telegram_user_id)
            if last_reminded and now - last_reminded < timedelta(hours=1):
                continue
            mentions.append(format_mention(binding.telegram_user_id, binding.telegram_full_name))
            reminded_user_ids.append(binding.telegram_user_id)
        if not mentions:
            continue
        message = (
            "War reminder: "
            + ", ".join(mentions)
            + " you still have attacks remaining."
        )
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send reminder to group %s: %s", group_id, exc)
            continue
        storage.set_cooldowns(group_id, reminded_user_ids, now)


async def main() -> None:
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()

    application.bot_data["storage"] = BindingsStorage(settings.bindings_db_path)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clan", clan))
    application.add_handler(CommandHandler("player", player))
    application.add_handler(CommandHandler("war", war))
    application.add_handler(CommandHandler("bind", bind))
    application.add_handler(CommandHandler("unbind", unbind))
    application.add_handler(CommandHandler("mytag", mytag))

    if settings.war_reminder_enabled:
        application.job_queue.run_repeating(
            war_reminder_job,
            interval=timedelta(minutes=settings.war_reminder_interval_minutes),
            first=timedelta(minutes=1),
            name="war-reminder",
        )

    logger.info("Telegram bot starting")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Telegram bot shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
