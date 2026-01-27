import asyncio
import html
import logging
import re
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.backend_client import build_url, fetch_json
from app.bindings_storage import Binding, BindingsStorage
from app.settings import settings, validate_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r"^[0289PYLQGRJCUV]+$")
TAG_EXTRACT_PATTERN = re.compile(r"#?[0289PYLQGRJCUV]{4,}")


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


class InvalidTagError(ValueError):
    pass


def normalize_tag(tag: str) -> str:
    cleaned = tag.replace(" ", "").strip().upper()
    if not cleaned.startswith("#"):
        cleaned = f"#{cleaned}"
    raw = cleaned.lstrip("#")
    if not raw or not TAG_PATTERN.fullmatch(raw):
        raise InvalidTagError("Invalid tag format")
    logger.info("Normalized tag input=%s normalized=%s", tag, cleaned)
    return cleaned


def encode_tag(tag: str) -> str:
    return quote(tag, safe="")


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
    if status == 403:
        return "Backend IP is not whitelisted for Clash of Clans."
    if status == 404:
        return "Player tag not found."
    if status == 429:
        return "Rate limit reached. Please try again later."
    if status == 504:
        return "Backend timed out contacting Clash of Clans."
    return "Backend error while validating player."


def ensure_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def ensure_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


def bind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Привязать", callback_data="bind_start")]]
    )


def bind_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Отмена", callback_data="bind_cancel")]]
    )


def extract_tag(text: str) -> str | None:
    match = TAG_EXTRACT_PATTERN.search(text.replace(" ", "").upper())
    if not match:
        return None
    return match.group(0)


async def handle_handler_exception(
    update: Update | None,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception("Unhandled handler error", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "Unexpected error occurred. Please try again."
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    try:
        if ensure_private_chat(update):
            await update.message.reply_text(
                "To continue, bind your account.",
                reply_markup=bind_keyboard(),
            )
            return
        await update.message.reply_text(
            "Welcome! Use /clan, /player <tag>, or /war to get Clash of Clans info."
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle /start")


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
            elif status == 401:
                message = "Backend token invalid. Please update the backend token."
            elif status == 403:
                message = "Backend IP is not whitelisted for Clash of Clans."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching clan data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /clan")
            await update.message.reply_text("Unexpected error occurred. Please try again.")


async def player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /player <tag>")
        return
    try:
        tag = normalize_tag(context.args[0])
    except InvalidTagError:
        await update.message.reply_text("Invalid player tag format.")
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, f"/player/{encode_tag(tag)}")
            await update.message.reply_text(format_player(payload), parse_mode=ParseMode.MARKDOWN)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 400:
                message = "Invalid player tag format."
            elif status == 404:
                message = "Player not found."
            elif status == 401:
                message = "Backend token invalid. Please update the backend token."
            elif status == 403:
                message = "Backend IP is not whitelisted for Clash of Clans."
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
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /player")
            await update.message.reply_text("Unexpected error occurred. Please try again.")


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
            elif status == 401:
                message = "Backend token invalid. Please update the backend token."
            elif status == 403:
                message = "Backend IP is not whitelisted for Clash of Clans."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching war data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /war")
            await update.message.reply_text("Unexpected error occurred. Please try again.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    try:
        await update.message.reply_text("pong")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to reply to /ping")


async def bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    try:
        if not ensure_private_chat(update):
            await update.message.reply_text("Please use /bind in a private chat with the bot.")
            return
        if not context.args:
            await update.message.reply_text(
                "Send your player tag (e.g. #2PRGP0L22).",
                reply_markup=bind_cancel_keyboard(),
            )
            context.user_data["awaiting_tag"] = True
            return
        raw_tag = " ".join(context.args)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle /bind")
        await update.message.reply_text("Unexpected error occurred. Please try again.")
        return
    await process_binding(update, context, raw_tag)


async def process_binding(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_tag: str,
) -> None:
    if not update.effective_user or not update.message:
        return
    context.user_data["awaiting_tag"] = False
    logger.info(
        "Bind request received user_id=%s group_id=%s raw_tag=%s",
        update.effective_user.id,
        settings.clan_group_id,
        raw_tag,
    )
    try:
        tag = normalize_tag(raw_tag)
    except InvalidTagError:
        await update.message.reply_text("Invalid player tag format.")
        return
    logger.info(
        "Bind normalized user_id=%s tag=%s",
        update.effective_user.id,
        tag,
    )
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            path = f"/player/{encode_tag(tag)}"
            response = await client.get(build_url(path))
            logger.info(
                "Bind backend validation user_id=%s status=%s",
                update.effective_user.id,
                response.status_code,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error validating player: %s", exc)
            if status == 404:
                await update.message.reply_text("Player tag not found.")
            elif status in {401, 403}:
                await update.message.reply_text(
                    "Backend authentication issue. Check API token/IP."
                )
            else:
                await update.message.reply_text(binding_error_message(status))
            return
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable during bind: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")
            return
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error during bind validation")
            await update.message.reply_text("Unexpected error occurred. Please try again.")
            return
    if settings.enforce_clan_membership:
        if not settings.coc_clan_tag:
            logger.error("Clan membership enforcement enabled without COC_CLAN_TAG set")
            await update.message.reply_text(
                "Binding unavailable: clan configuration missing."
            )
            return
        player_clan = payload.get("clan", {}).get("tag")
        try:
            if not player_clan or normalize_tag(player_clan) != normalize_tag(settings.coc_clan_tag):
                await update.message.reply_text("Player is not in this clan.")
                logger.info(
                    "Bind rejected user_id=%s tag=%s clan_tag=%s",
                    update.effective_user.id,
                    tag,
                    player_clan,
                )
                return
        except InvalidTagError:
            await update.message.reply_text("Player is not in this clan.")
            return
    storage: BindingsStorage = context.application.bot_data["storage"]
    now = datetime.now(timezone.utc).isoformat()
    binding = Binding(
        telegram_user_id=update.effective_user.id,
        group_id=settings.clan_group_id or 0,
        coc_player_tag=tag,
        telegram_username=update.effective_user.username,
        telegram_full_name=update.effective_user.full_name,
        created_at=now,
    )
    try:
        storage.upsert_binding(binding)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bind DB write failed user_id=%s error=%s", update.effective_user.id, exc)
        await update.message.reply_text("Internal error while saving binding.")
        return
    logger.info(
        "Bind saved user_id=%s group_id=%s tag=%s",
        update.effective_user.id,
        binding.group_id,
        tag,
    )
    mention = format_mention(update.effective_user.id, update.effective_user.full_name)
    try:
        if settings.clan_group_id is None:
            await update.message.reply_text(
                f"Bound {mention} to {html.escape(tag)} successfully.\n"
                "Clan group is not configured yet.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        expire_at = datetime.now(timezone.utc) + timedelta(minutes=settings.invite_ttl_minutes)
        invite = await context.bot.create_chat_invite_link(
            chat_id=settings.clan_group_id,
            expire_date=expire_at,
            member_limit=1,
        )
        logger.info(
            "Invite created user_id=%s chat_id=%s expire_at=%s",
            update.effective_user.id,
            settings.clan_group_id,
            expire_at.isoformat(),
        )
        await update.message.reply_text(
            f"Bound {mention} to {html.escape(tag)} successfully.\n"
            f"Here is your invite link (valid for {settings.invite_ttl_minutes} minutes):\n"
            f"{invite.invite_link}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Invite creation failed user_id=%s error=%s", update.effective_user.id, exc)
        await update.message.reply_text(
            f"Bound {mention} to {html.escape(tag)} successfully.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def unbind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    try:
        if not ensure_private_chat(update):
            await update.message.reply_text("Please use /unbind in a private chat with the bot.")
            return
        storage: BindingsStorage = context.application.bot_data["storage"]
        removed = storage.delete_binding(settings.clan_group_id or 0, update.effective_user.id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle /unbind")
        await update.message.reply_text("Unexpected error occurred. Please try again.")
        return
    if removed:
        mention = format_mention(update.effective_user.id, update.effective_user.full_name)
        message = f"Removed binding for {mention}."
    else:
        message = "No binding found for your account."
    await update.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def mytag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    try:
        if not ensure_private_chat(update):
            await update.message.reply_text("Please use /mytag in a private chat with the bot.")
            return
        storage: BindingsStorage = context.application.bot_data["storage"]
        binding = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
        if not binding:
            await update.message.reply_text("No tag bound for your account.")
            return
        await update.message.reply_text(
            f"Your bound tag is {html.escape(binding.coc_player_tag)}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle /mytag")
        await update.message.reply_text("Unexpected error occurred. Please try again.")


async def bind_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_user:
        return
    try:
        await update.callback_query.answer()
        if not ensure_private_chat(update):
            await update.callback_query.edit_message_text(
                "Please use /bind in a private chat with the bot."
            )
            return
        context.user_data["awaiting_tag"] = True
        await update.callback_query.edit_message_text(
            "Send your player tag (e.g. #2PRGP0L22).",
            reply_markup=bind_cancel_keyboard(),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle bind_start callback")


async def bind_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    try:
        await update.callback_query.answer()
        context.user_data.pop("awaiting_tag", None)
        await update.callback_query.edit_message_text("Binding cancelled.")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle bind_cancel callback")


async def capture_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not ensure_private_chat(update):
        return
    if not context.user_data.get("awaiting_tag"):
        return
    try:
        raw_input = update.message.text or ""
        extracted = extract_tag(raw_input)
        if not extracted:
            await update.message.reply_text(
                "Invalid player tag format. Example: #2PRGP0L22",
                reply_markup=bind_cancel_keyboard(),
            )
            return
        context.user_data["awaiting_tag"] = False
        await process_binding(update, context, extracted)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to capture tag message")
        await update.message.reply_text("Unexpected error occurred. Please try again.")


async def verify_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.new_chat_members:
        return
    if update.effective_chat is None:
        return
    if settings.clan_group_id is None or update.effective_chat.id != settings.clan_group_id:
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        binding = storage.get_binding(settings.clan_group_id, member.id)
        if not binding:
            try:
                await update.message.reply_text(
                    "You must bind first using /bind in private chat."
                )
                await context.bot.ban_chat_member(update.effective_chat.id, member.id)
                await context.bot.unban_chat_member(update.effective_chat.id, member.id)
                logger.info(
                    "Unbound member removed user_id=%s chat_id=%s",
                    member.id,
                    update.effective_chat.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to remove unbound member user_id=%s error=%s",
                    member.id,
                    exc,
                )
            continue
        mention = format_mention(member.id, member.full_name)
        message = f"{mention} joined as {html.escape(binding.coc_player_tag)}"
        try:
            await update.message.reply_text(
                message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to announce member user_id=%s error=%s", member.id, exc)


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
    tags_missing_attacks: set[str] = set()
    for member in clan_members:
        if attacks_used(member) != 0 or not member.get("tag"):
            continue
        try:
            tags_missing_attacks.add(normalize_tag(member["tag"]))
        except InvalidTagError:
            logger.warning("Skipping invalid member tag in war payload")
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
    missing = validate_settings()
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        raise SystemExit(1)
    logger.info("Environment validation passed")

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()

    application.bot_data["storage"] = BindingsStorage(settings.bindings_db_path)

    application.add_handler(MessageHandler(filters.COMMAND, log_any_command), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clan", clan))
    application.add_handler(CommandHandler("player", player))
    application.add_handler(CommandHandler("war", war))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("bind", bind))
    application.add_handler(CommandHandler("unbind", unbind))
    application.add_handler(CommandHandler("mytag", mytag))
    application.add_handler(CallbackQueryHandler(bind_start, pattern="^bind_start$"))
    application.add_handler(CallbackQueryHandler(bind_cancel, pattern="^bind_cancel$"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, capture_tag)
    )
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, verify_new_members))
    application.add_error_handler(handle_handler_exception)

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
