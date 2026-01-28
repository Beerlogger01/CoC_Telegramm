import asyncio
import html
import os
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
from app.settings import env_snapshot, settings, settings_snapshot, validate_settings

log_level = logging.DEBUG if settings.debug else logging.INFO
logging.basicConfig(level=log_level, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
logger.debug("Debug mode enabled")

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


def format_settings_report() -> str:
    lines = [
        "Bot settings snapshot:",
    ]
    for key, value in settings_snapshot().items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


async def log_any_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    logger.info(
        "Command received user_id=%s chat_id=%s text=%s",
        update.effective_user.id if update.effective_user else None,
        update.effective_chat.id if update.effective_chat else None,
        update.message.text,
    )


def bind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Привязать", callback_data="bind_start")]]
    )


def bind_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Отмена", callback_data="bind_cancel")]]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu with all bot functions."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Топ игроков", callback_data="menu_topplayers"),
            InlineKeyboardButton("⚔️ Статистика клана", callback_data="menu_clanstats"),
        ],
        [
            InlineKeyboardButton("🏘️ Информация о клане", callback_data="menu_clan"),
            InlineKeyboardButton("📊 Война", callback_data="menu_war"),
        ],
        [
            InlineKeyboardButton("👤 Информация об игроке", callback_data="menu_player"),
            InlineKeyboardButton("⚙️ Привязка", callback_data="bind_start"),
        ],
    ])


async def send_or_edit_message(update: Update, text: str, parse_mode: str = ParseMode.MARKDOWN, reply_markup = None) -> None:
    """Send message for regular command or edit for callback query."""
    if update.message:
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)



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
        logger.debug("Start command from user %s", update.effective_user.id)
        if ensure_private_chat(update):
            await update.message.reply_text(
                "To continue, bind your account.",
                reply_markup=bind_keyboard(),
            )
            return
        await update.message.reply_text(
            "Welcome! Use /clan, /player <tag>, or /war to get Clash of Clans info."
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to handle /start: %s", e, exc_info=settings.debug)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main menu with all bot functions."""
    if not update.message:
        return
    try:
        await update.message.reply_text(
            "🎮 *Меню бота*\n\n"
            "Выберите функцию:",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to handle /menu: %s", e, exc_info=settings.debug)


async def clan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message and not update.callback_query:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/clan")
            await send_or_edit_message(update, format_clan(payload))
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
            await send_or_edit_message(update, message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await send_or_edit_message(update, "Backend is unreachable. Please try again later.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /clan")
            await send_or_edit_message(update, "Unexpected error occurred. Please try again.")


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
    if not update.message and not update.callback_query:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/war")
            await send_or_edit_message(update, format_war(payload))
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
            await send_or_edit_message(update, message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await send_or_edit_message(update, "Backend is unreachable. Please try again later.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /war")
            await send_or_edit_message(update, "Unexpected error occurred. Please try again.")


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
                "Clan group is not configured yet. "
                "Ask an admin to set CLAN_GROUP_ID (use /chatid in the group).",
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


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu button callbacks."""
    if not update.callback_query:
        return
    
    callback_data = update.callback_query.data
    
    try:
        await update.callback_query.answer()
        
        # Route to appropriate handler
        if callback_data == "menu_topplayers":
            context.args = []
            await top_players(update, context)
        elif callback_data == "menu_clanstats":
            await clan_stats(update, context)
        elif callback_data == "menu_clan":
            await clan(update, context)
        elif callback_data == "menu_war":
            await war(update, context)
        elif callback_data == "menu_player":
            # For player, we need to ask for tag
            await update.callback_query.edit_message_text(
                "Пожалуйста отправьте тег игрока (например: #ABC123DEF)\n"
                "Или используйте /player <tag>"
            )
        else:
            await update.callback_query.edit_message_text("Неизвестная команда")
    except Exception as e:
        logger.error("Menu callback error: %s", e, exc_info=settings.debug)


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


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if ensure_group_chat(update):
        await update.message.reply_text(
            f"Chat ID: `{update.effective_chat.id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(
        "Please run /chatid in the target group chat to obtain its ID."
    )


async def grouplink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send group invite link to bound members (if they lost it)."""
    if not update.message or not update.effective_user:
        return
    if not ensure_private_chat(update):
        return
    
    try:
        # Check if user is bound
        storage: BindingsStorage = context.application.bot_data["storage"]
        binding = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
        
        if not binding:
            await update.message.reply_text(
                "❌ Вы не привязаны к боту. Используйте /bind чтобы привязаться."
            )
            return
        
        if settings.clan_group_id is None:
            await update.message.reply_text(
                "⚠️ Группа клана ещё не настроена. Свяжитесь с администратором."
            )
            return
        
        # Generate new invite link
        expire_at = datetime.now(timezone.utc) + timedelta(minutes=settings.invite_ttl_minutes)
        invite = await context.bot.create_chat_invite_link(
            chat_id=settings.clan_group_id,
            expire_date=expire_at,
            member_limit=1,
        )
        
        logger.info(
            "Group link sent user_id=%s tag=%s expire_at=%s",
            update.effective_user.id,
            binding.coc_player_tag,
            expire_at.isoformat(),
        )
        
        await update.message.reply_text(
            f"✅ Ссылка для присоединения к группе клана:\n"
            f"{invite.invite_link}\n\n"
            f"⏱️ Ссылка действительна {settings.invite_ttl_minutes} минут",
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send group link: %s", exc)
        await update.message.reply_text(
            "❌ Ошибка при создании ссылки. Попробуйте позже."
        )



async def settings_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not ensure_private_chat(update):
        await update.message.reply_text("Please use /settings in a private chat with the bot.")
        return
    await update.message.reply_text(format_settings_report())


async def verify_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verify that new members are bound to the bot before joining clan group."""
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
                # Send warning message with bind button
                mention = format_mention(member.id, member.full_name)
                await update.message.reply_text(
                    f"⚠️ {mention} попытался присоединиться, но не привязан к боту!\n\n"
                    f"Чтобы присоединиться к группе клана, нужно:\n"
                    f"1. Написать боту @{context.bot.username} в личные сообщения\n"
                    f"2. Нажать кнопку 'Привязать' и ввести свой тег\n\n"
                    f"После привязки вы сможете присоединиться к группе.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                # Remove from group (ban then unban to kick)
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
        
        # Member is bound - welcome them
        mention = format_mention(member.id, member.full_name)
        player_tag = html.escape(binding.coc_player_tag)
        message = f"✅ {mention} присоединился как {player_tag}"
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


async def log_any_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        logger.info("Command received: %s from user %s", update.message.text, update.effective_user.id)


async def top_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top 10 clan members by trophies."""
    if not update.message and not update.callback_query:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            limit = 10
            if context.args and context.args[0].isdigit():
                limit = min(int(context.args[0]), 50)
            
            payload = await fetch_json(client, f"/top-players?limit={limit}")
            clan_name = payload.get("clanName", "Clan")
            members = payload.get("members", [])
            
            lines = [f"*Top {len(members)} Players in {clan_name}*\n"]
            for i, member in enumerate(members, 1):
                name = member.get("name", "Unknown")
                trophies = member.get("trophies", 0)
                th = member.get("townHallLevel", "?")
                lines.append(f"{i}. {name} - {trophies} 🏆 (TH{th})")
            
            message = "\n".join(lines)
            await send_or_edit_message(update, message)
        except httpx.HTTPStatusError as exc:
            logger.warning("Backend error: %s", exc)
            await send_or_edit_message(update, "Failed to fetch top players. Try again later.")
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await send_or_edit_message(update, "Backend is unreachable.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /top-players")
            await send_or_edit_message(update, "Unexpected error occurred.")


async def clan_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show clan statistics and war info."""
    if not update.message and not update.callback_query:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            clan_payload = await fetch_json(client, "/clan")
            war_payload = await fetch_json(client, "/war")
            
            clan_msg = format_clan(clan_payload)
            
            war_state = war_payload.get("state", "notInWar")
            if war_state == "inWar":
                our_team = war_payload.get("clan", {})
                enemy = war_payload.get("opponent", {})
                our_destruction = our_team.get("destructionPercentage", 0)
                enemy_destruction = enemy.get("destructionPercentage", 0)
                
                war_info = (
                    f"\n*Current War Status*\n"
                    f"Our Team: {our_team.get('name', 'N/A')} - {our_destruction:.1f}% destruction\n"
                    f"Enemy: {enemy.get('name', 'N/A')} - {enemy_destruction:.1f}% destruction\n"
                )
                clan_msg += war_info
            
            await send_or_edit_message(update, clan_msg)
        except httpx.HTTPStatusError as exc:
            logger.warning("Backend error: %s", exc)
            await send_or_edit_message(update, "Failed to fetch clan stats.")
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await send_or_edit_message(update, "Backend is unreachable.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /clan-stats")
            await send_or_edit_message(update, "Unexpected error occurred.")


async def ai_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with AI-generated text when bot is mentioned or replied-to using Groq API or fallback."""
    if not update.message or not update.message.text:
        logger.debug("ai_reply_handler: no message or text")
        return

    text = update.message.text
    logger.debug(f"ai_reply_handler: processing text: {text[:100]}")
    
    # Detect if bot is mentioned
    bot_username = None
    try:
        bot_username = (context.bot.username or "").lower()
        logger.debug(f"ai_reply_handler: bot username = {bot_username}")
    except Exception as e:
        logger.debug(f"ai_reply_handler: failed to get bot username: {e}")
        bot_username = os.getenv("BOT_USERNAME", "").lower()

    mentioned = False
    if bot_username and f"@{bot_username}" in (text or "").lower():
        mentioned = True
        logger.info(f"ai_reply_handler: bot mentioned directly in text")
    
    # Check if replying to bot's message
    if not mentioned and update.message.reply_to_message:
        try:
            replied_to_bot = (
                update.message.reply_to_message.from_user and
                update.message.reply_to_message.from_user.id == context.bot.id
            )
            if replied_to_bot:
                mentioned = True
                logger.info(f"ai_reply_handler: replying to bot message")
        except Exception as e:
            logger.debug(f"ai_reply_handler: error checking reply: {e}")

    if not mentioned:
        logger.debug("ai_reply_handler: bot not mentioned, returning")
        return

    logger.info(f"ai_reply_handler: attempting AI reply for text: {text[:100]}")
    
    # Try Groq API (free, fast)
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        logger.info("ai_reply_handler: attempting Groq API")
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            
            # Use the fastest free model available
            response = client.chat.completions.create(
                model="mixtral-8x7b-32768",  # Free tier model
                messages=[
                    {"role": "system", "content": "You are a helpful Telegram bot assistant. Respond concisely in the same language as the user."},
                    {"role": "user", "content": text}
                ],
                max_tokens=256,
                temperature=0.7,
            )
            
            reply = response.choices[0].message.content.strip()
            if reply:
                logger.info(f"ai_reply_handler: Groq reply: {reply[:100]}")
                await update.message.reply_text(reply)
                return
        except Exception as exc:
            logger.warning(f"ai_reply_handler: Groq failed: {exc}")

    # Try OpenAI if key is set
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        logger.info("ai_reply_handler: attempting OpenAI")
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}
                body = {
                    "model": "gpt-3.5-turbo",
                    "messages": [
                        {"role": "system", "content": "You are a helpful Telegram bot assistant. Respond concisely."},
                        {"role": "user", "content": text}
                    ],
                    "max_tokens": 256,
                }
                resp = await client.post("https://api.openai.com/v1/chat/completions", json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                reply = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if reply:
                    logger.info(f"ai_reply_handler: OpenAI reply: {reply[:100]}")
                    await update.message.reply_text(reply)
                    return
        except Exception as exc:
            logger.warning(f"ai_reply_handler: OpenAI failed: {exc}")

    # Fallback
    logger.warning("ai_reply_handler: using fallback reply")
    safe_reply = f"Я упомянут! Вы написали: {text[:400]}"
    await update.message.reply_text(safe_reply)


async def main() -> None:
    logger.info("Bot environment snapshot: %s", env_snapshot())
    logger.info("Bot settings snapshot: %s", settings_snapshot())
    missing = validate_settings()
    if missing:
        logger.error("Settings validation failed:\n- %s", "\n- ".join(missing))
        logger.error(
            "Fix the missing/invalid environment variables and restart the bot."
        )
        raise SystemExit(1)
    if settings.clan_group_id is None:
        logger.warning(
            "CLAN_GROUP_ID is not set. Invite links and member verification are disabled. "
            "Add the bot to the clan group and run /chatid to obtain the ID."
        )
    logger.info("Environment validation passed")

    from telegram.request import HTTPXRequest
    request = HTTPXRequest(read_timeout=settings.request_timeout_seconds)
    application = ApplicationBuilder().token(settings.telegram_bot_token).request(request).build()

    application.bot_data["storage"] = BindingsStorage(settings.bindings_db_path)

    application.add_handler(MessageHandler(filters.COMMAND, log_any_command), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("clan", clan))
    application.add_handler(CommandHandler("clanstats", clan_stats))
    application.add_handler(CommandHandler("topplayers", top_players))
    application.add_handler(CommandHandler("player", player))
    application.add_handler(CommandHandler("war", war))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("bind", bind))
    application.add_handler(CommandHandler("unbind", unbind))
    application.add_handler(CommandHandler("mytag", mytag))
    application.add_handler(CommandHandler("chatid", chatid))
    application.add_handler(CommandHandler("grouplink", grouplink))
    application.add_handler(CommandHandler("settings", settings_info))
    application.add_handler(CallbackQueryHandler(bind_start, pattern="^bind_start$"))
    application.add_handler(CallbackQueryHandler(bind_cancel, pattern="^bind_cancel$"))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    # AI mention handler: replies when the bot is mentioned or replied-to
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_reply_handler))

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
