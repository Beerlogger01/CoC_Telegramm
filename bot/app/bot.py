import asyncio
import html
import os
import logging
import re
from urllib.parse import quote
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
from telegram.constants import ChatType, ParseMode
from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.error import TelegramError

from app.widgets import Widgets, dispatch_widget, WidgetUpdateProcessor
from app.backend_client import build_url, fetch_json
from app.bindings_storage import Binding, BindingsStorage
from app.settings import env_snapshot, settings, settings_snapshot, validate_settings
from app.ui import format_war_message, format_raid_message, lineup_message, lineup_keyboard, war_keyboard
from app.tags import assign_game_tag, sync_tags_daily
from app.binding_ui import sorted_roster, roster_keyboard, available_roster
from app.captain import is_captain, captain_report_text
from app.players_ui import roster_page, roster_keyboard as players_roster_keyboard, donation_text, donation_keyboard, hall_text
from app.profile_ui import (profile_text, profile_keyboard, detail_text, detail_keyboard,
                            delete_keyboard, progress_text, progress_keyboard, progress_changes)
from app.polls import PollController, PollStore
from app.operations import Operations, WarPlanStore
from app.reminders import ReminderStore
from app.views import home_keyboard, hub_screen, cwl_screen, warlog_screen, war_review, welcome_text
from app.navigation import (navigator, callback_data, acknowledge, back_buttons,
                            ignore_duplicate_update, page_index, cancel_input_modes)
from app.war_results import WarAnnouncementStore, classify_result, format_result, recently_finished, war_identity

log_level = logging.DEBUG if settings.debug else logging.INFO
logging.basicConfig(level=log_level, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
logger.debug("Debug mode enabled")
# httpx records full Telegram bot tokens in request URLs at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Scrub Telegram bot tokens from every formatted log line and traceback.
TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


class SecretRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return TOKEN_IN_URL.sub("/bot[REDACTED]", super().format(record))


for _handler in logging.getLogger().handlers:
    _handler.setFormatter(SecretRedactingFormatter("%(levelname)s %(name)s %(message)s"))


TAG_PATTERN = re.compile(r"^[0289PYLQGRJCUV]+$")
TAG_EXTRACT_PATTERN = re.compile(r"#?[0289PYLQGRJCUV]{4,}")


def format_clan(payload: dict[str, Any]) -> str:
    esc = lambda v: html.escape(str(v if v is not None else 'нет данных'))
    return (f"🏰 <b>{esc(payload.get('name'))}</b>\n"
            f"Тег: <code>{esc(payload.get('tag'))}</code>\n"
            f"Уровень клана: {esc(payload.get('clanLevel'))}\n"
            f"Игроков: {esc(payload.get('members'))} / 50\n"
            f"Лига ЛВК: {esc((payload.get('warLeague') or {}).get('name'))}")


def format_player(payload: dict[str, Any]) -> str:
    league = payload.get('leagueTier') or payload.get('league') or {}
    return detail_text({**payload, 'leagueName': league.get('name')})



def format_war(payload: dict[str, Any]) -> str:
    """Render a localised war summary."""
    return format_war_message(payload)


def format_activity_report(payload: dict[str, Any]) -> str:
    return captain_report_text(payload)


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
        return "Неверный формат тега игрока."
    if status == 401:
        return "Сервис игры временно недоступен. Сообщи руководству."
    if status == 403:
        return "Сервис игры временно недоступен. Сообщи руководству."
    if status == 404:
        return "Игрок с таким тегом не найден."
    if status == 429:
        return "Слишком много запросов к игре. Попробуй немного позже."
    if status == 504:
        return "Игра отвечает дольше обычного. Попробуй позже."
    return "Не удалось проверить игрока. Попробуй позже."


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
    cancel_input_modes(context)
    logger.info(
        "Command received user_id=%s chat_id=%s text=%s",
        update.effective_user.id if update.effective_user else None,
        update.effective_chat.id if update.effective_chat else None,
        (update.message.text or "").split(" ", 1)[0],
    )


def bind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Привязать", callback_data="bind_start")]]
    )


def bind_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Отмена", callback_data="bind_cancel")]]
    )


def main_menu_keyboard(user_id=None, captain=False):
    return home_keyboard(captain)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Штаб клана", callback_data="menu_home")]])


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = (
        "❓ <b>Помощь · Troublemakers</b>\n\n"
        "/menu — главное меню с кнопками\n"
        "/war — текущая война и атаки\n"
        "/nextwar — подбор состава и история\n"
        "/donate — пожертвования сезона\n"
        "/hall — зал славы клана\n"
        "/cwl — 7 войн ЛВК и место по звёздам\n"
        "/rating [war|cwl] — рейтинг всего клана\n"
        "/ready — готовность каждого аккаунта к конкретной войне\n"
        "/lineup — сохранённые капитаном составы\n"
        "/history — история и разбор атак\n"
        "/poll — создать голосование (для руководства, в личке)\n"
        "/nextwar war 30 — состав КВ 30 × 30\n"
        "/progress — развитие любого своего аккаунта\n"
        "/captain — отчёт для руководства клана\n"
        "/clan — данные клана\n"
        "/topplayers — список игроков с карточками\n"
        "/raids — рейды столицы\n"
        "/profile — мой игровой профиль\n"
        "/bind — привязать дополнительный аккаунт\n"
        "/accounts — мои аккаунты и твинки\n"
        "/primary #ТЕГ — выбрать основной\n"
        "/unlink #ТЕГ — удалить одну привязку\n"
        "/weights — таблица весов\n"
        "/staff — меню главы и соруководителей\n"
        "/lastseen, /setweight, /adminunlink — для руководства\n"
        "/grouplink — получить приглашение\n"
        "/unbind confirm — отвязать все аккаунты\n"
        "/chatid — ID группы (для администратора)\n\n"
        "Личность игрока подтверждается игровым API Token; его нельзя "
        "путать с ключом разработчика."
    )
    await send_or_edit_message(update, lines, parse_mode=ParseMode.HTML, reply_markup=back_keyboard())


async def sync_bot_commands(bot) -> None:
    """Replace stale BotFather /start /stop /help menu commands."""
    base = [BotCommand("menu", "🏠 Штаб клана"),
            BotCommand("war", "⚔️ Текущая война"),
            BotCommand("nextwar", "📋 Следующий состав"),
            BotCommand("cwl", "🏆 Лига CWL"),
            BotCommand("rating", "📊 Рейтинг клана"),
            BotCommand("ready", "✅ Моя готовность"),
            BotCommand("lineup", "📋 Сохранённые составы"),
            BotCommand("history", "📜 Разбор войн"),
            BotCommand("poll", "🗳 Создать голосование"),
            BotCommand("progress", "📈 Прогресс игрока"),
            BotCommand("staff", "🛡️ Управление кланом"),
            BotCommand("donate", "🎁 Донат сезона"),
            BotCommand("hall", "🏆 Зал славы"),
            BotCommand("profile", "👤 Мой профиль"),
            BotCommand("clan", "🏰 Информация о клане"),
            BotCommand("help", "❓ Помощь")]
    await bot.set_my_commands(base)
    await bot.set_my_commands(
        base + [BotCommand("bind", "🔐 Добавить аккаунт"),
                BotCommand("accounts", "👥 Мои аккаунты"),
                BotCommand("weights", "📊 Веса отбора"),
                BotCommand("grouplink", "🔗 Ссылка в группу")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [BotCommand("menu", "🏠 Штаб клана"),
         BotCommand("war", "⚔️ Текущая война"),
         BotCommand("nextwar", "📋 Следующий состав"),
         BotCommand("cwl", "🏆 Результаты CWL"),
         BotCommand("rating", "📊 Рейтинг клана"),
         BotCommand("raids", "🏛️ Рейды столицы"),
         BotCommand("help", "❓ Помощь")],
        scope=BotCommandScopeAllGroupChats(),
    )


async def send_or_edit_message(update: Update, text: str, parse_mode=ParseMode.HTML, reply_markup=None):
    return await navigator.render(update, text, parse_mode, reply_markup)



def extract_tag(text: str) -> str | None:
    """Extract a valid-looking CoC player tag from private text."""
    match = TAG_EXTRACT_PATTERN.search(text.replace(" ", "").upper())
    return match.group(0) if match else None


async def handle_handler_exception(
    update: Update | None,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception("Unhandled handler error", exc_info=context.error)
    if update and update.effective_message:
        try:
            await send_or_edit_message(update, "Не удалось открыть этот экран. Попробуй ещё раз через штаб.")
        except TelegramError:
            logger.warning("Could not deliver error screen")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    try:
        logger.debug("Start command from user %s", update.effective_user.id)
        if ensure_private_chat(update):
            storage: BindingsStorage = context.application.bot_data["storage"]
            linked = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
            if linked:
                await menu(update, context)
                return
            # Private chat - show bind option
            user_name = update.effective_user.first_name or "Player"
            await send_or_edit_message(update,
                f"👋 Привет, {html.escape(user_name)}!\n\n"
                f"Я — бот Clash of Clans для нашего клана.\n\n"
                f"🔐 Чтобы присоединиться к группе клана, нужно:\n"
                f"1️⃣ Привязать свой аккаунт\n"
                f"2️⃣ Получить ссылку на группу\n"
                f"3️⃣ Присоединиться\n\n"
                f"Начнём с привязки!",
                reply_markup=bind_keyboard(),
            )
            return
        # Group chat
        await send_or_edit_message(update,
            f"🎮 Привет! Напиши мне в личку (@{context.bot.username}), чтобы привязать игровой аккаунт."
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to handle /start: %s", e, exc_info=settings.debug)


async def has_captain_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Revalidate role on every privileged action, not just when rendering buttons."""
    if not ensure_private_chat(update) or not update.effective_user:
        return False
    storage: BindingsStorage = context.application.bot_data["storage"]
    bindings = storage.get_user_accounts(settings.clan_group_id or 0, update.effective_user.id)
    if not bindings:
        return False
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            clan_data = await fetch_json(client, "/clan")
        members = clan_data.get("memberList") or []
        return any(is_captain(members, account.coc_player_tag) for account in bindings)
    except (httpx.RequestError, httpx.HTTPStatusError):
        return False


async def staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """CoC-leadership-only actions, based on ANY of the owner's verified alts."""
    if not await has_captain_access(update, context):
        await send_or_edit_message(update, "🛡️ Доступ только главе и соруководителям CoC.")
        return
    rows = [
        [InlineKeyboardButton("⚔️ КВ · состав", callback_data="menu_nextwar"),
         InlineKeyboardButton("🏆 ЛВК · раунды", callback_data="menu_cwl")],
        [InlineKeyboardButton("📈 Повышения", callback_data="staff:promotion"),
         InlineKeyboardButton("⚠️ Дисциплина", callback_data="staff:kick")],
        [InlineKeyboardButton("🎖 Бонусы CWL", callback_data="staff:bonus"),
         InlineKeyboardButton("📊 Отчёт", callback_data="menu_captain")],
        [InlineKeyboardButton("⚖️ Веса", callback_data="menu_weights"),
         InlineKeyboardButton("👥 Привязки", callback_data="menu_accounts")],
        [InlineKeyboardButton("📋 Сохранённые составы", callback_data="menu_ops"),
         InlineKeyboardButton("🗳 Голосования", callback_data="menu_polls")],
        [InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")],
    ]
    await send_or_edit_message(
        update,
        "🛡️ <b>Кабинет руководства CoC</b>\n"
        "Права определяются по максимальному званию среди всех твоих "
        "подтверждённых игровых аккаунтов. Админ Telegram не требуется.\n\n"
        "<b>Команды:</b> /setweight war quality 40 · /lastseen #ТЕГ 3 · "
        "/adminunlink #ТЕГ · /captain\n"
        "Кики, повышения и бонусы выполняются в игре вручную.",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
    )


async def staff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    await acknowledge(update)
    if not await has_captain_access(update, context):
        await send_or_edit_message(update, "Недостаточно прав CoC.")
        return
    stage = (callback_data(update) or "staff:kick").split(":")[1]
    mode = "cwl" if stage == "bonus" else context.user_data.get("lineup_mode", "war")
    data = context.user_data.get("lineup_snapshot")
    if not data or data.get("mode") != mode:
        context.user_data["lineup_mode"] = mode
        context.user_data["lineup_target_stage"] = stage
        await next_war_analysis(update, context)
        return
    stage = "promotion" if stage == "promotion" else "bonus" if stage == "bonus" else "kick"
    await send_or_edit_message(
        update, lineup_message(data, stage), parse_mode=ParseMode.HTML,
        reply_markup=lineup_keyboard(stage, data=data, captain=True),
    )


async def captain_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await has_captain_access(update, context):
        await send_or_edit_message(update, "Этот раздел доступен только лидеру или соруководителю клана.")
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            data = await fetch_json(client, "/activity-report")
        await send_or_edit_message(update, captain_report_text(data), parse_mode=ParseMode.HTML, reply_markup=back_buttons("menu_staff"))
    except (httpx.RequestError, httpx.HTTPStatusError):
        await send_or_edit_message(update, "Не удалось собрать отчёт.")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a single navigable dashboard in private chats and groups."""
    if not update.effective_user:
        return
    cancel_input_modes(context)
    widgets = context.application.bot_data.get("widgets")
    if widgets and await widgets.open(update, context):
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    binding = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
    who = html.escape(binding.coc_player_tag) if binding else "не привязан"
    name = html.escape(settings.coc_clan_tag or "Troublemakers")
    greeting = html.escape(update.effective_user.first_name or "Игрок")
    text = f"🏰 <b>Штаб клана</b>\n{name}\n\nПривет, {greeting}!\n👤 Основной аккаунт: <code>{who}</code>\nВыбери раздел:"
    captain = await has_captain_access(update, context) if binding else False
    await send_or_edit_message(update, text, parse_mode=ParseMode.HTML,
                               reply_markup=main_menu_keyboard(captain=captain))


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle first message in private chat - offer binding."""
    if not update.message or not update.effective_user or not update.message.text:
        return

    # Only for private chats
    if not ensure_private_chat(update):
        return

    # Skip if user is waiting for tag input (capture_tag will handle it)
    if context.user_data.get("awaiting_tag") or context.user_data.get("awaiting_player_token"):
        return

    # Skip if user is already bound
    storage: BindingsStorage = context.application.bot_data["storage"]
    binding = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
    if binding:
        await menu(update, context)
        return

    # User not bound - offer binding
    if not context.user_data.get("binding_offered"):
        context.user_data["binding_offered"] = True
        user_name = update.effective_user.first_name or "Player"
        await send_or_edit_message(update,
            f"👋 Привет, {html.escape(user_name)}!\n\n"
            f"Я — бот Clash of Clans для нашего клана.\n\n"
            f"🔐 Чтобы присоединиться к группе клана, нужно сначала привязать свой аккаунт.\n\n"
            f"Нажми 'Привязать' 👇",
            reply_markup=bind_keyboard(),
            parse_mode=ParseMode.HTML,
        )


async def clan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clan summary with consistent back-to-dashboard navigation."""
    if not update.message and not update.callback_query:
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            payload = await fetch_json(client, "/clan")
        await send_or_edit_message(
            update, format_clan(payload), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 Зал славы", callback_data="menu_hall"),
                 InlineKeyboardButton("🎁 Донат", callback_data="menu_donate")],
                [InlineKeyboardButton("👥 Игроки", callback_data="menu_topplayers")],
                [InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")],
            ]),
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("Clan API request failed status=%s", exc.response.status_code)
        await send_or_edit_message(update, binding_error_message(exc.response.status_code))
    except httpx.RequestError:
        logger.warning("Clan API unavailable")
        await send_or_edit_message(update, "Сервис данных клана временно недоступен.")


async def player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show an explicitly supplied tag or the account linked to this Telegram user."""
    if not update.message and not update.callback_query:
        return
    if not update.effective_user:
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    if context.args and context.args[0].lower() not in ("я", "me"):
        try:
            tag = normalize_tag(context.args[0])
        except InvalidTagError:
            await send_or_edit_message(update, "Неверный формат тега.")
            return
    else:
        bound = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
        if not bound:
            await send_or_edit_message(update, "Сначала привяжи игровой аккаунт через /bind.")
            return
        tag = bound.coc_player_tag
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            payload = await fetch_json(client, f"/player/{encode_tag(tag)}")
        league = payload.get("leagueTier") or payload.get("league") or {}
        payload["leagueName"] = league.get("name") if isinstance(league, dict) else None
        context.user_data["inspected_player"] = payload
        await send_or_edit_message(update, detail_text(payload), reply_markup=detail_keyboard(0,False,payload,own=False,inspect=True))
    except httpx.HTTPStatusError as exc:
        logger.warning("Player API request failed status=%s", exc.response.status_code)
        await send_or_edit_message(update, binding_error_message(exc.response.status_code))
    except httpx.RequestError:
        logger.warning("Player API unavailable")
        await send_or_edit_message(update, "Сервис данных игрока временно недоступен.")


async def inspect_callback(update, context):
    await acknowledge(update)
    payload = context.user_data.get("inspected_player")
    if not payload:
        await send_or_edit_message(update,"Карточка устарела. Открой игрока снова."); return
    _,section,_,*rest = callback_data(update).split(":")
    section = "info" if section == "view" else section
    page = int(rest[0]) if rest else 0
    await send_or_edit_message(update,detail_text(payload,section=section,page=page),
                               reply_markup=detail_keyboard(0,False,payload,section,page,own=False,inspect=True))


async def warpage_callback(update,context):
    await acknowledge(update)
    payload = context.user_data.get("current_war_snapshot")
    if payload is None:
        await war(update,context); return
    page = int(callback_data(update).split(":")[1])
    await send_or_edit_message(update,format_war_message(payload,page),reply_markup=war_keyboard(payload,page))


async def war(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message and not update.callback_query:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/war")
            context.user_data["current_war_snapshot"] = payload
            await send_or_edit_message(update, format_war(payload), parse_mode=ParseMode.HTML, reply_markup=war_keyboard(payload))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 429:
                message = "Слишком много запросов к игре. Попробуй немного позже."
            elif status == 400:
                message = "Не удалось найти настроенный клан. Сообщи руководству."
            elif status == 401:
                message = "Сервис игры временно недоступен. Сообщи руководству."
            elif status == 403:
                message = "Сервис игры временно недоступен. Сообщи руководству."
            elif status == 504:
                message = "Игра отвечает дольше обычного. Попробуй позже."
            else:
                message = "Не удалось загрузить сведения о войне."
            await send_or_edit_message(update, message, reply_markup=back_buttons("menu_wars"))
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await send_or_edit_message(update, "Сервис данных временно недоступен. Попробуй позже.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /war")
            await send_or_edit_message(update, "Не удалось выполнить действие. Попробуй ещё раз через штаб.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    try:
        await send_or_edit_message(update, "pong")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to reply to /ping")


async def bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not ensure_private_chat(update):
        await send_or_edit_message(update, "Привязка доступна только в личном чате.")
        return
    if context.args:
        await process_binding(update, context, " ".join(context.args))
    else:
        await show_player_picker(update, context)


async def show_player_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Select from the real clan roster; verification is still mandatory."""
    if not ensure_private_chat(update) or not update.effective_user:
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            clan_data = await fetch_json(client, "/clan")
        storage: BindingsStorage = context.application.bot_data["storage"]
        group_id = settings.clan_group_id or 0
        roster = clan_data.get("memberList") or []
        linked = storage.get_bindings_for_tags(
            group_id, [str(member["tag"]) for member in roster if member.get("tag")],
        )
        # Already linked accounts must not be offered to new or returning users.
        # This includes the current user's own alts: manage them in /profile.
        taken = {account.coc_player_tag for account in linked}
        members = sorted_roster(
            available_roster(roster, taken),
            update.effective_user.full_name,
            update.effective_user.username or "",
        )
        if not members:
            context.user_data["picker_members"] = []
            context.user_data["awaiting_tag"] = False
            await send_or_edit_message(
                update,
                "🔒 Все участники клана уже привязаны к Telegram. "
                "Если хочешь управлять своими аккаунтами, открой /profile. "
                "Если привязка устарела, руководитель может удалить её через /adminunlink.",
                parse_mode=None, reply_markup=roster_keyboard([]),
            )
            return
        context.user_data["picker_members"] = members
        context.user_data["awaiting_tag"] = False
        await send_or_edit_message(
            update,
            "🔐 <b>Выбери свой аккаунт</b>\n"
            "Это список участников клана. Первый вариант может совпадать с твоим именем Telegram.\n"
            "Выбор ника НЕ является доказательством владения аккаунтом.",
            parse_mode=ParseMode.HTML,
            reply_markup=roster_keyboard(members),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
        logger.warning("Clan roster unavailable for binding picker")
        context.user_data["awaiting_tag"] = True
        await send_or_edit_message(update, "Введи свой тег игрока, например #VU099JCR.",
                                   parse_mode=None, reply_markup=bind_cancel_keyboard())


async def roster_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not ensure_private_chat(update):
        return
    await acknowledge(update)
    command = callback_data(update) or ""
    parts = command.split(":")
    if len(parts) != 3:
        return
    kind, index_str = parts[1:]
    idx = int(index_str)
    if kind == "manual":
        context.user_data["awaiting_tag"] = True
        await send_or_edit_message(update,
            "✍️ Введи игровой тег, например #VU099JCR.",
            reply_markup=bind_cancel_keyboard())
        return
    members = context.user_data.get("picker_members") or []
    if not members:
        await send_or_edit_message(update, "Список устарел. Введи /bind.")
        return
    if kind == "page":
        await send_or_edit_message(update, "🔐 <b>Выбери свой аккаунт</b>", reply_markup=roster_keyboard(members, idx))
        return
    if kind != "player" or not 0 <= idx < len(members):
        return
    member = members[idx]
    # The keyboard may be hours old: another user could have linked this tag
    # since it was rendered. Never proceed to token verification for a taken tag.
    storage: BindingsStorage = context.application.bot_data["storage"]
    group_id = settings.clan_group_id or 0
    if storage.get_user_id_by_tag(group_id, str(member["tag"])) is not None:
        await send_or_edit_message(update,
            "🔒 Этот игровой аккаунт уже привязан. Обновляю список свободных игроков...")
        await show_player_picker(update, context)
        return
    await send_or_edit_message(update,
        f"Выбран {html.escape(str(member.get('name') or '?'))} · {html.escape(member['tag'])}. Проверяю аккаунт...")
    await process_binding(update, context, str(member["tag"]))


async def process_binding(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_tag: str,
    verified: bool = False,
) -> None:
    if not update.effective_user or not update.effective_message:
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
        await send_or_edit_message(update, "Неверный формат тега игрока.")
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
                await send_or_edit_message(update, "Игрок с таким тегом не найден.")
            elif status in {401, 403}:
                await send_or_edit_message(update,
                    "Игра временно не принимает запросы. Сообщи руководству."
                )
            else:
                await send_or_edit_message(update, binding_error_message(status), reply_markup=bind_cancel_keyboard())
            return
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable during bind: %s", exc)
            await send_or_edit_message(update, "Сервис данных временно недоступен. Попробуй позже.")
            return
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error during bind validation")
            await send_or_edit_message(update, "Не удалось выполнить действие. Попробуй ещё раз через штаб.")
            return
    if settings.enforce_clan_membership:
        if not settings.coc_clan_tag:
            logger.error("Clan membership enforcement enabled without COC_CLAN_TAG set")
            await send_or_edit_message(update,
                "Привязка пока недоступна: клан не настроен."
            )
            return
        player_clan = payload.get("clan", {}).get("tag")
        try:
            if not player_clan or normalize_tag(player_clan) != normalize_tag(settings.coc_clan_tag):
                await send_or_edit_message(update, "Этот аккаунт сейчас не состоит в нашем клане.")
                logger.info(
                    "Bind rejected user_id=%s tag=%s clan_tag=%s",
                    update.effective_user.id,
                    tag,
                    player_clan,
                )
                return
        except InvalidTagError:
            await send_or_edit_message(update, "Этот аккаунт сейчас не состоит в нашем клане.")
            return
    if settings.require_player_verification and not verified:
        context.user_data["pending_coc_tag"] = tag
        context.user_data["awaiting_player_token"] = True
        await send_or_edit_message(update,
            "🔐 Для проверки владения аккаунтом открой Clash of Clans → "
            "Настройки → Ещё настройки → API Token и пришли токен игрока сюда.\n"
            "Это НЕ ключ разработчика. Сообщение с токеном бот попытается удалить.",
            reply_markup=bind_cancel_keyboard(),
        )
        return

    storage: BindingsStorage = context.application.bot_data["storage"]
    owner_id = storage.get_user_id_by_tag(settings.clan_group_id or 0, tag)
    if owner_id is not None and owner_id != update.effective_user.id:
        await send_or_edit_message(update,
            "Игровой аккаунт уже привязан к другому пользователю. Обратитесь к администратору."
        )
        return
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
        await send_or_edit_message(update, "Не удалось сохранить привязку. Попробуй позже.")
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
            await send_or_edit_message(update,
                f"✅ {mention}, аккаунт {html.escape(tag)} привязан.\n"
                "Группа клана ещё не настроена. "
                "Сообщи руководству.",
                parse_mode=ParseMode.HTML,
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
        await send_or_edit_message(update,
            f"✅ {mention}, аккаунт {html.escape(tag)} привязан.\n"
            f"Приглашение в группу (действует {settings.invite_ttl_minutes} мин):\n"
            f"{invite.invite_link}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Invite creation failed user_id=%s error=%s", update.effective_user.id, exc)
        await send_or_edit_message(update,
            f"✅ {mention}, аккаунт {html.escape(tag)} привязан. Приглашение можно запросить через /grouplink.",
            parse_mode=ParseMode.HTML,
        )


async def unbind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not ensure_private_chat(update):
        return
    if not context.args or context.args[0].lower() != "confirm":
        await send_or_edit_message(update,
            "⚠️ Эта команда удалит ВСЕ твои игровые привязки, включая твинков.\n"
            "Для удаления одного аккаунта используй /unlink #ТЕГ.\n"
            "Для подтверждения всех: /unbind confirm"
        )
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    removed = storage.delete_binding(settings.clan_group_id or 0, update.effective_user.id)
    await send_or_edit_message(update, "Все привязанные аккаунты удалены." if removed
                                    else "У тебя нет привязанных аккаунтов.")


async def mytag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    try:
        if not ensure_private_chat(update):
            await send_or_edit_message(update, "Открой /mytag в личке бота.")
            return
        storage: BindingsStorage = context.application.bot_data["storage"]
        binding = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
        if not binding:
            await send_or_edit_message(update, "У тебя пока нет привязанного аккаунта. Открой /bind.")
            return
        await send_or_edit_message(update,
            f"Твой основной аккаунт: {html.escape(binding.coc_player_tag)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle /mytag")
        await send_or_edit_message(update, "Не удалось выполнить действие. Попробуй ещё раз через штаб.")


async def _load_owned_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Read names/stats live from CoC; never treat Telegram display name as proof."""
    storage: BindingsStorage = context.application.bot_data["storage"]
    bound = storage.get_user_accounts(settings.clan_group_id or 0, update.effective_user.id)
    if not bound:
        return []
    semaphore = asyncio.Semaphore(3)
    async def fetch_one(account):
        tag = account.coc_player_tag
        async with semaphore:
            try:
                async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                    result = await fetch_json(client, f"/player/{encode_tag(tag)}")
            except (httpx.HTTPStatusError, httpx.RequestError):
                result = {"name": tag, "unavailable": True}
        result["tag"] = tag
        league = result.get("leagueTier") or result.get("league") or {}
        result["leagueName"] = league.get("name") if isinstance(league, dict) else None
        result["isPrimary"] = False
        return result
    profiles = await asyncio.gather(*(fetch_one(account) for account in bound))
    primary = storage.get_binding(settings.clan_group_id or 0, update.effective_user.id)
    for profile in profiles:
        profile["isPrimary"] = bool(primary and profile["tag"] == primary.coc_player_tag)
    context.user_data["own_profiles"] = profiles
    return profiles


async def accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A single overview is used by /accounts, /profile and 'Мой профиль'."""
    if not update.effective_user:
        return
    if not ensure_private_chat(update):
        await send_or_edit_message(update, "Личные игровые аккаунты доступны в личном чате с ботом.")
        return
    profiles = await _load_owned_profiles(update, context)
    if not profiles:
        await send_or_edit_message(
            update, "У тебя нет привязанных игровых аккаунтов. Добавь первый через /bind.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Привязать", callback_data="bind_start")],
                [InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")],
            ]),
        )
        return
    primary = next((p["tag"] for p in profiles if p.get("isPrimary")), None)
    page = page_index(context.user_data.get("accounts_page",0),len(profiles),5)
    await send_or_edit_message(update, profile_text(profiles, primary, page),
                               parse_mode=ParseMode.HTML,
                               reply_markup=profile_keyboard(profiles, page))


async def account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only verified account owner can inspect, switch or delete an account."""
    query = update.callback_query
    if not query or not ensure_private_chat(update) or not update.effective_user:
        return
    await acknowledge(update)
    _, action, value, *extra = (callback_data(update) or "").split(":")
    if action == "list":
        context.user_data["accounts_page"] = int(value)
        await accounts(update, context)
        return
    profiles = context.user_data.get("own_profiles") or []
    if not profiles:
        profiles = await _load_owned_profiles(update, context)
    try:
        index = int(value)
    except ValueError:
        return
    if not 0 <= index < len(profiles):
        await send_or_edit_message(update, "Список устарел, открой /profile.")
        return
    selected = profiles[index]
    tag = selected["tag"]
    storage: BindingsStorage = context.application.bot_data["storage"]
    authorized = {account.coc_player_tag for account in
                  storage.get_user_accounts(settings.clan_group_id or 0, update.effective_user.id)}
    if tag not in authorized:
        await send_or_edit_message(update, "Аккаунт больше не привязан к твоему Telegram.")
        return
    if action in {"view", "heroes", "equipment"}:
        section = "info" if action == "view" else action
        page = int(extra[0]) if extra else 0
        await send_or_edit_message(update, detail_text(selected, bool(selected.get("isPrimary")), section, page),
                                   parse_mode=ParseMode.HTML,
                                   reply_markup=detail_keyboard(index, bool(selected.get("isPrimary")), selected, section, page))
    elif action == "primary":
        storage.set_primary_account(settings.clan_group_id or 0, update.effective_user.id, tag)
        await accounts(update, context)
    elif action == "delete":
        await send_or_edit_message(
            update,
            f"🗑 Удалить привязку <b>{html.escape(str(selected.get('name') or tag))}</b>?\n"
            "Игровой аккаунт и Telegram-группа останутся без изменений.",
            parse_mode=ParseMode.HTML, reply_markup=delete_keyboard(index),
        )
    elif action == "confirm":
        success = storage.unlink_account(settings.clan_group_id or 0, tag,
                                         owner_id=update.effective_user.id)
        if success:
            await accounts(update, context)
        else:
            await send_or_edit_message(update, "Привязка уже удалена или принадлежит другому пользователю.")
    elif action == "progress":
        context.user_data["progress_page"] = 0
        context.user_data["progress_index"] = index
        await show_owned_progress(update, context)


async def primary_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not ensure_private_chat(update):
        return
    if len(context.args) != 1:
        await send_or_edit_message(update, "Используй /primary #ТЕГ")
        return
    try:
        tag = normalize_tag(context.args[0])
    except InvalidTagError:
        await send_or_edit_message(update, "Неверный тег игрока.")
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    if not storage.set_primary_account(settings.clan_group_id or 0, update.effective_user.id, tag):
        await send_or_edit_message(update, "Этот аккаунт тебе не принадлежит или не привязан.")
        return
    await send_or_edit_message(update, f"⭐ {tag} выбран основным. Остальные твинковые привязки сохранены.")


async def unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not ensure_private_chat(update):
        return
    if len(context.args) != 1:
        await send_or_edit_message(update, "Используй /unlink #ТЕГ. Удаляется только одна привязка.")
        return
    try:
        tag = normalize_tag(context.args[0])
    except InvalidTagError:
        await send_or_edit_message(update, "Неверный тег.")
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    success = storage.unlink_account(settings.clan_group_id or 0, tag,
                                     owner_id=update.effective_user.id)
    await send_or_edit_message(update,
        f"Привязка {tag} удалена из базы. Остальные аккаунты сохранены." if success
        else "Привязка не найдена или принадлежит другому пользователю."
    )


async def admin_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A verified leader or co-leader can release one account without kicking anyone."""
    if not update.message or not update.effective_user or not ensure_private_chat(update):
        return
    if not await has_captain_access(update, context):
        await send_or_edit_message(update, "Только лидер или соруководитель клана.")
        return
    if len(context.args) != 1:
        await send_or_edit_message(update, "Используй /adminunlink #ТЕГ")
        return
    try:
        tag = normalize_tag(context.args[0])
    except InvalidTagError:
        await send_or_edit_message(update, "Неверный тег.")
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    success = storage.unlink_account(settings.clan_group_id or 0, tag)
    logger.info("Admin unlink actor=%s tag=%s success=%s", update.effective_user.id, tag, success)
    await send_or_edit_message(update,
        f"Привязка {tag} удалена. Игрок НЕ исключён из клана или группы." if success
        else "Привязка с таким тегом отсутствует."
    )


async def weights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = (context.args or ["war"])[0].lower()
    if mode not in {"war", "cwl"}:
        mode = "war"
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(build_url("/selection/weights"), params={"mode": mode})
            response.raise_for_status()
            values = response.json().get("weights") or {}
    except (httpx.HTTPStatusError, httpx.RequestError):
        await send_or_edit_message(update, "Не удалось загрузить веса отбора.")
        return
    labels = [("strength", "🏰 Сила TH и прокачка"),
              ("discipline", "🎯 Дисциплина атак"),
              ("quality", "⭐ Качество атак с учётом цели"),
              ("role", "🛡️ Звание CoC")]
    lines = [f"📊 <b>Веса {'ЛВК' if mode == 'cwl' else 'КВ'}</b>", ""]
    for key, name in labels:
        lines.append(f"{name}: <b>{float(values.get(key, 0)):g}%</b>")
    lines += ["", "Сумма: 100%. Красная отметка CoC запрещает КВ и ЛВК.",
              "Нет истории: низкая уверенность, не ноль эффективности.",
              "Руководителям: /setweight cwl quality 45"]
    await send_or_edit_message(update, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_buttons("menu_staff"))


async def set_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not ensure_private_chat(update):
        return
    if not await has_captain_access(update, context):
        await send_or_edit_message(update, "Настраивать веса может только лидер или соруководитель CoC.")
        return
    if len(context.args) != 3:
        await send_or_edit_message(update, "Пример: /setweight war quality 40\n"
                                        "war/cwl; strength discipline quality role.")
        return
    mode, metric, value_s = (v.lower() for v in context.args)
    if mode not in {"war", "cwl"} or metric not in {"strength", "discipline", "quality", "role"}:
        await send_or_edit_message(update, "Неверный режим или метрика.")
        return
    try:
        value = float(value_s)
        if not 0 <= value <= 100:
            raise ValueError
    except ValueError:
        await send_or_edit_message(update, "Нужен процент от 0 до 100.")
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(build_url("/selection/weights"),
                                         json={"mode": mode, "metric": metric, "value": value})
            response.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError):
        await send_or_edit_message(update, "Не удалось сохранить веса.")
        return
    await send_or_edit_message(update, f"✅ Веса {mode.upper()} обновлены.")
    context.args = [mode]
    await weights(update, context)


async def last_seen_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual observation from in-game leader list, NEVER an inferred API last-online."""
    if not update.message or not update.effective_user or not ensure_private_chat(update):
        return
    if not await has_captain_access(update, context):
        await send_or_edit_message(update, "Ручное время входа может указывать только руководство.")
        return
    if len(context.args) != 2:
        await send_or_edit_message(update,
            "Используй /lastseen #ТЕГ 3 — если в игре показано «3 дня назад».\n"
            "Для очистки: /lastseen #ТЕГ clear\n"
            "CoC API не предоставляет точное время последнего входа."
        )
        return
    try:
        tag = normalize_tag(context.args[0])
        days = None if context.args[1].lower() == "clear" else float(context.args[1])
        if days is not None and (not 0 <= days <= 30):
            raise ValueError
    except (InvalidTagError, ValueError):
        await send_or_edit_message(update, "Укажи корректный тег и число дней от 0 до 30 либо clear.")
        return
    # Do not record non-clan members or unverified nickname guesses.
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            roster = await fetch_json(client, "/clan")
        if tag not in {m.get("tag") for m in roster.get("memberList") or []}:
            await send_or_edit_message(update, "Игрока с этим тегом сейчас нет в клане.")
            return
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(days=days)).isoformat() if days is not None else None
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            resp = await client.post(build_url(f"/selection/lastseen/{encode_tag(tag)}"),
                                     json={"last_seen_at": stamp,
                                           "reported_by": update.effective_user.id})
            resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError):
        await send_or_edit_message(update, "Не удалось сохранить отметку активности.")
        return
    message = (f"Ручная отметка последнего входа {tag}: {days:g} дн. назад."
               if days is not None else f"Для {tag} ручная отметка очищена.")
    await send_or_edit_message(update, message + "\nИсточник: наблюдение руководства в игре, НЕ CoC API.")



async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    await acknowledge(update)
    action = callback_data(update)
    cancel_input_modes(context)
    if action in {'menu_wars', 'menu_ratings', 'menu_community'}:
        text, keyboard = hub_screen(action[5:])
        await send_or_edit_message(update, text, reply_markup=keyboard)
        return
    if action in {'menu_ratingwar', 'menu_ratingcwl'}:
        context.args = ['war' if action == 'menu_ratingwar' else 'cwl']
        await clan_rating(update, context)
        return
    handlers = {
        "menu_home": menu,
        "menu_topplayers": top_players,
        "menu_donate": donations,
        "menu_hall": hall_of_fame,
        "menu_clan": clan,
        "menu_raids": clan_raids,
        "menu_games": clan_games,
        "menu_war": war,
        "menu_nextwar": next_war_analysis,
        "menu_weights": weights,
        "menu_cwl": cwl,
        "menu_rating": clan_rating,
        "menu_poll": create_poll,
        "menu_polls": poll_list,
        "menu_ops": operations,
        "menu_warlog": war_history,
        "menu_progress": progress,
        "menu_captain": captain_report,
        "menu_staff": staff,
        "menu_player": player,
        "menu_profile": accounts,
        "menu_accounts": accounts,
        "menu_help": help_command,
    }
    if action in handlers:
        context.args = []
        await handlers[action](update, context)
    elif action == "menu_report":
        await send_or_edit_message(update, "Отчёт доступен только капитану.")
    else:
        await send_or_edit_message(update,
            "Неизвестный раздел.", reply_markup=back_keyboard())


async def bind_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    await acknowledge(update)
    if not ensure_private_chat(update):
        await send_or_edit_message(update, "Привязка доступна в личном чате бота.")
        return
    await show_player_picker(update, context)


async def bind_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    try:
        await acknowledge(update)
        context.user_data.pop("awaiting_tag", None)
        context.user_data.pop("awaiting_player_token", None)
        context.user_data.pop("pending_coc_tag", None)
        context.user_data.pop("picker_members", None)
        await send_or_edit_message(update, "Привязка отменена.")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to handle bind_cancel callback")


async def private_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ensure a private message is processed only once."""
    if context.user_data.get("awaiting_tag") or context.user_data.get("awaiting_player_token"):
        await capture_tag(update, context)
    elif context.user_data.get("plan_title"):
        await context.application.bot_data["operations"].title_text(update, context)
    elif context.user_data.get("poll_draft"):
        await context.application.bot_data["polls"].text(update, context)
    else:
        await handle_private_message(update, context)


async def capture_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not ensure_private_chat(update):
        return
    if context.user_data.get("awaiting_player_token"):
        await verify_player_ownership(update, context)
        return
    if not context.user_data.get("awaiting_tag"):
        return
    try:
        raw_input = update.message.text or ""
        extracted = extract_tag(raw_input)
        if not extracted:
            await send_or_edit_message(update,
                "Неверный формат тега игрока. Пример: #2PRGP0L22",
                reply_markup=bind_cancel_keyboard(),
            )
            return
        # Only clear awaiting_tag after successful processing
        await process_binding(update, context, extracted)
        context.user_data["binding_offered"] = False
    except Exception:  # noqa: BLE001
        logger.exception("Failed to capture tag message")
        # Don't clear awaiting_tag - let user try again
        await send_or_edit_message(update,
            "Не удалось выполнить действие. Попробуй ещё раз через штаб.",
            reply_markup=bind_cancel_keyboard(),
        )


async def verify_player_ownership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verify an in-game player token and never persist it."""
    if not update.message or not update.effective_chat:
        return
    tag = context.user_data.get("pending_coc_tag")
    if not tag:
        context.user_data.pop("awaiting_player_token", None)
        await send_or_edit_message(update, "Сессия привязки истекла. Отправьте /bind.")
        return
    player_token = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not player_token or len(player_token) > 256:
        await send_or_edit_message(update, "Неверный формат токена.")
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(
                build_url(f"/verify-player/{encode_tag(tag)}"),
                json={"token": player_token},
            )
            response.raise_for_status()
            valid = response.json().get("verified") is True
    except httpx.HTTPStatusError as exc:
        logger.warning("Player verification failed: HTTP %s", exc.response.status_code)
        await send_or_edit_message(update, "Ошибка проверки игрока. Повторите позже.")
        return
    except httpx.RequestError:
        logger.warning("Player verification service unreachable")
        await send_or_edit_message(update, "Сервис проверки временно недоступен.")
        return
    finally:
        player_token = None
    if not valid:
        await send_or_edit_message(
            update, "❌ Токен не подходит выбранному игроку. Попробуйте ещё раз."
        )
        return
    context.user_data.pop("awaiting_player_token", None)
    context.user_data.pop("pending_coc_tag", None)
    await process_binding(update, context, tag, verified=True)


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if ensure_group_chat(update):
        await send_or_edit_message(update,
            f"Chat ID: `{update.effective_chat.id}`",
            parse_mode=ParseMode.HTML,
        )
        return
    await send_or_edit_message(update,
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
            await send_or_edit_message(update,
                "❌ Вы не привязаны к боту. Используйте /bind чтобы привязаться."
            )
            return

        if settings.clan_group_id is None:
            await send_or_edit_message(update,
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

        await send_or_edit_message(update,
            f"✅ Ссылка для присоединения к группе клана:\n"
            f"{invite.invite_link}\n\n"
            f"⏱️ Ссылка действительна {settings.invite_ttl_minutes} минут",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send group link: %s", exc)
        await send_or_edit_message(update,
            "❌ Ошибка при создании ссылки. Попробуйте позже."
        )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await accounts(update, context)


async def settings_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not ensure_private_chat(update):
        await send_or_edit_message(update, "Открой /settings в личке бота.")
        return
    await send_or_edit_message(update, "⚙️ Настройки: привязка через /bind, изменение через /unbind.\n"
                                   "Параметры сервера хранятся в Home Assistant и не показываются участникам.")


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

        # Member is bound - welcome them with CoC info
        mention = format_mention(member.id, member.full_name)

        # Try to fetch player info for nickname
        coc_name = None
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                path = f"/player/{quote(binding.coc_player_tag)}"
                response = await client.get(build_url(path))
                if response.status_code == 200:
                    player_data = response.json()
                    coc_name = player_data.get("name", "")
        except Exception:  # noqa: BLE001
            pass  # Fallback if API fails

        if coc_name:
            await assign_game_tag(context, update.effective_chat.id, member.id, coc_name)

        message, phrase = welcome_text(mention, coc_name or member.full_name, binding.coc_player_tag,
                                      context.application.bot_data.get('last_welcome_phrase'))
        context.application.bot_data['last_welcome_phrase'] = phrase
        try:
            await update.message.reply_text(
                message, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        except Exception:
            logger.warning("Could not send welcome message for member user_id=%s", member.id)


async def weekly_activity_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send weekly activity report every Sunday to Lex."""
    # Check if it's Sunday (weekday() = 6)
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:  # Sunday
        return

    # Get Lex's user ID from database using their CoC tag
    if not settings.lex_coc_tag:
        logger.warning("LEX_COC_TAG not configured for weekly activity report")
        return

    storage: BindingsStorage = context.application.bot_data["storage"]
    lex_user_id = storage.get_user_id_by_tag(settings.clan_group_id or 0, settings.lex_coc_tag)
    if not lex_user_id:
        logger.warning("Lex not found in bindings by tag=%s", settings.lex_coc_tag)
        return

    await send_activity_report_to_user(context, lex_user_id)


async def send_activity_report_to_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Send activity report to specific user."""
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/activity-report")
            message = format_activity_report(payload)
            await context.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode=ParseMode.HTML,
                )
            logger.info("Activity report sent to user %s", user_id)
        except httpx.HTTPStatusError as exc:
            logger.warning("Backend error fetching activity report: %s", exc)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable for activity report: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send activity report: %s", exc)


async def war_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.war_reminder_enabled:
        return
    storage: BindingsStorage = context.application.bot_data["storage"]
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/war")
        except (httpx.HTTPStatusError, httpx.RequestError):
            logger.warning("War data unavailable for reminders")
            return
    if payload.get("state") != "inWar": return
    end_time = parse_coc_time(payload.get("endTime"))
    identity = war_identity(payload)
    if end_time is None or identity is None: return
    remaining_time = end_time - datetime.now(timezone.utc)
    if not timedelta(0) < remaining_time <= timedelta(hours=settings.war_reminder_window_hours): return
    stage = "last30m" if remaining_time <= timedelta(minutes=30) else "window"
    allowed = int(payload.get("attacksPerMember") or 1)
    missing = {}
    for member in (payload.get("clan") or {}).get("members") or []:
        if attacks_used(member) >= allowed or not member.get("tag"): continue
        try:
            missing[normalize_tag(member["tag"])] = member
        except InvalidTagError:
            continue
    store = context.application.bot_data["reminders"]
    for group_id in storage.get_group_ids():
        linked = storage.get_bindings_for_tags(group_id, missing)
        grouped = {}
        for account in linked: grouped.setdefault(account.telegram_user_id, []).append(account)
        owners = store.claim(group_id,identity,stage,list(grouped))
        if not owners: continue
        lines = [f"⚔️ <b>Остались атаки · до конца {max(1,int(remaining_time.total_seconds()//60))} мин</b>"]
        for owner in owners:
            accounts = grouped[owner]
            display = format_mention(owner, accounts[0].telegram_full_name[:24])
            games = [f"{html.escape(str(missing[a.coc_player_tag].get('name') or a.coc_player_tag)[:24])} ({allowed-attacks_used(missing[a.coc_player_tag])})" for a in accounts]
            lines.append(f"{display}: " + ", ".join(games))
        # Chunk only when needed; every account stays visible. Claims survive restart.
        chunks, current, length = [], [], 0
        for line in lines:
            from app.navigation import plain_text
            width = len(plain_text(line).encode('utf-16-le'))//2 + 1
            if current and length + width > 3800:
                chunks.append("\n".join(current)); current, length = [], 0
            current.append(line); length += width
        if current: chunks.append("\n".join(current))
        for message in chunks:
            try:
                await context.bot.send_message(chat_id=group_id,text=message,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
            except TelegramError:
                logger.warning("Reminder delivery uncertain for group %s; no automatic resend",group_id)



async def war_result_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Announce each observed completed war at most once to the configured group.

    The first successful fetch records existing finished wars without announcing
    them. Deduplication and startup baseline persist across Home Assistant restarts.
    """
    if not settings.war_results_enabled or settings.clan_group_id is None:
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await fetch_json(client, "/war-results")
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning("Cannot retrieve completed war results; retrying next polling cycle")
        return
    wars = response.get("wars") or []
    # Only complete, attributable results get reserved. Never announce an unknown
    # or in-progress result, or a war involving another clan.
    by_id: dict[str, dict[str, Any]] = {}
    for war in reversed(wars):  # older results first
        identity = war_identity(war)
        if identity and classify_result(war, settings.coc_clan_tag) is not None:
            by_id[identity] = war

    store: WarAnnouncementStore = context.application.bot_data["war_outcomes"]
    ids = store.claim_new(settings.clan_group_id, list(by_id))
    for war_id in ids:
        war = by_id[war_id]
        if not recently_finished(war):
            continue  # Don't wake the group about old archived rounds.
        try:
            await context.bot.send_message(
                chat_id=settings.clan_group_id,
                text=format_result(war, settings.coc_clan_tag),
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
        except Exception:
            # Never log a raw Telegram API exception with an embedded bot token.
            # The ID stays reserved: better miss one message than spam on restart.
            logger.warning("War outcome announcement failed for id=%s", war_id)


def format_top_players(payload: dict[str, Any]) -> str:
    """Safe HTML rendering; activity metrics are not a last-online timestamp."""
    lines = [
        "👥 <b>Активность участников клана</b>",
        "<i>Пожертвования и атаки, не время последнего входа.</i>",
    ]
    for title, key in (
        ("🏆 <b>Лидеры по активности</b>", "mostActive"),
        ("📉 <b>Меньше всего доната / атак</b>", "leastActive"),
    ):
        lines.extend(("", title))
        members = (payload.get(key) or [])[:10]
        if not members:
            lines.append("Данных пока нет.")
        for index, player in enumerate(members, 1):
            # HTML names must be escaped, and each dynamic field is bounded.
            # Never truncate final HTML: cutting inside &amp; or <b> breaks Telegram.
            name = html.escape(str(player.get("name") or "Игрок")[:24])
            th = html.escape(str(player.get("townHallLevel") or "?")[:8])
            donations = html.escape(str(player.get("donations") if player.get("donations") is not None else 0)[:12])
            attacks = html.escape(str(player.get("warAttacks") if player.get("warAttacks") is not None else 0)[:12])
            lines.append(
                f"{index}. <b>{name}</b> · TH{th}\n"
                f"    💰 {donations} доната · ⚔️ {attacks} атак"
            )
    return "\n".join(lines)


async def top_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show every clan member and allow opening API-backed player profiles."""
    if not update.effective_user:
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            clan_data = await fetch_json(client, "/clan")
        roster = sorted(
            [m for m in clan_data.get("memberList") or [] if m.get("tag")],
            key=lambda m: (str(m.get("name") or "").casefold(), str(m.get("tag"))),
        )
        context.user_data["clan_player_roster"] = roster
        await send_or_edit_message(
            update, roster_page(roster), parse_mode=ParseMode.HTML,
            reply_markup=players_roster_keyboard(roster),
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning("Could not load member directory")
        await send_or_edit_message(update, "Список игроков пока недоступен.")


async def donations(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    roster = context.user_data.get("clan_player_roster")
    if roster is None:
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                clan_data = await fetch_json(client, "/clan")
            roster = clan_data.get("memberList") or []
            context.user_data["clan_player_roster"] = roster
        except (httpx.HTTPStatusError, httpx.RequestError):
            await send_or_edit_message(update, "Не удалось получить текущий донат.")
            return
    await send_or_edit_message(update, donation_text(roster, page), parse_mode=ParseMode.HTML,
                               reply_markup=donation_keyboard(len(roster), page))


async def hall_of_fame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            payload = await fetch_json(client, "/hall")
        await send_or_edit_message(update, hall_text(payload), parse_mode=ParseMode.HTML,
                                   reply_markup=back_buttons("menu_ratings"))
    except (httpx.HTTPStatusError, httpx.RequestError):
        await send_or_edit_message(update, "Зал славы пока недоступен: данные не получены.")


async def players_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    await acknowledge(update)
    _, action, raw_index, *extra = (callback_data(update) or "").split(":")
    if action == "donate":
        await donations(update, context, int(raw_index))
        return
    if action == "hall":
        await hall_of_fame(update, context)
        return
    roster = context.user_data.get("clan_player_roster")
    if roster is None:
        await top_players(update, context)
        return
    index = int(raw_index)
    if action == "page":
        await send_or_edit_message(update, roster_page(roster, index),
                                   parse_mode=ParseMode.HTML,
                                   reply_markup=players_roster_keyboard(roster, index))
        return
    if action not in {"view", "heroes", "equipment"} or not 0 <= index < len(roster):
        return
    tag = roster[index]["tag"]
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            profile_data = await fetch_json(client, f"/player/{encode_tag(tag)}")
        league = profile_data.get("leagueTier") or profile_data.get("league") or {}
        profile_data["leagueName"] = league.get("name") if isinstance(league, dict) else None
        section = "info" if action == "view" else action
        page = int(extra[0]) if extra else 0
        await send_or_edit_message(update, detail_text(profile_data, section=section, page=page), parse_mode=ParseMode.HTML,
                                   reply_markup=detail_keyboard(index, False, profile_data, section, page, own=False))
    except (httpx.HTTPStatusError, httpx.RequestError):
        await send_or_edit_message(update, "Не удалось загрузить профиль игрока.", reply_markup=back_buttons(f"players:page:{index//8}"))


async def clan_raids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message and not update.callback_query:
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            payload = await fetch_json(client, "/raids")
        await send_or_edit_message(update, format_raid_message(payload), parse_mode=ParseMode.HTML, reply_markup=back_buttons("menu_community"))
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.exception("Raid API unavailable")
        await send_or_edit_message(update, "🏛️ Не удалось получить данные о рейдах.")


async def clan_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show clan games (Clan Games) status."""
    if not update.message and not update.callback_query:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/games")

            # If no data or no games info, send message
            if not payload or "currentGames" not in payload:
                message = "ℹ️ Информация об играх кланов недоступна."
                await send_or_edit_message(update, message)
                return

            current_games = payload.get("currentGames")
            if not current_games:
                message = ("ℹ️ Clash of Clans API не предоставляет данные об Играх кланов."
                           if payload.get("unsupported") else "ℹ️ Информация об играх недоступна.")
                await send_or_edit_message(update, message)
                return

            # Format games info
            state = current_games.get("state", "unknown")
            start_time = current_games.get("startTime", "N/A")
            end_time = current_games.get("endTime", "N/A")

            if state == "inProgress":
                # Show current score
                score = current_games.get("score", "N/A")

                msg = "🎮 *Игры кланов*\n\n"
                msg += "*Статус:* Идут в данный момент 🏁\n"
                msg += f"*Начало:* {start_time}\n"
                msg += f"*Конец:* {end_time}\n"
                msg += f"*Очки:* {score}\n"
            else:
                # Show status when not in progress
                msg = "🎮 *Игры кланов*\n\n"
                msg += "*Статус:* Не проводятся\n"
                msg += f"*Начало:* {start_time}\n"
                msg += f"*Конец:* {end_time}\n"

            await send_or_edit_message(update, msg)
        except httpx.HTTPStatusError as exc:
            logger.warning("Backend error: %s", exc)
            await send_or_edit_message(update, "ℹ️ Информация об играх недоступна.")
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await send_or_edit_message(update, "Сервис данных временно недоступен.")
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in /clan-raids")
            await send_or_edit_message(update, "Не удалось выполнить действие. Попробуй ещё раз через штаб.")


async def ai_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with AI-generated text when bot is mentioned or replied-to using Groq API or fallback."""
    if not update.message or not update.message.text or ensure_private_chat(update):
        return

    text = update.message.text

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
        logger.info("ai_reply_handler: bot mentioned directly in text")

    # Check if replying to bot's message
    if not mentioned and update.message.reply_to_message:
        try:
            replied_to_bot = (
                update.message.reply_to_message.from_user and
                update.message.reply_to_message.from_user.id == context.bot.id
            )
            if replied_to_bot:
                mentioned = True
                logger.info("ai_reply_handler: replying to bot message")
        except Exception as e:
            logger.debug(f"ai_reply_handler: error checking reply: {e}")

    if not mentioned:
        logger.debug("ai_reply_handler: bot not mentioned, returning")
        return

    logger.info("AI reply requested in chat %s", update.effective_chat.id if update.effective_chat else "?")

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


async def show_owned_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render selected alt and interval, using independent persisted tag history."""
    if not update.effective_user or not ensure_private_chat(update):
        return
    profiles = context.user_data.get("own_profiles") or await _load_owned_profiles(update, context)
    if not profiles:
        await send_or_edit_message(update, "Сначала привяжи игровой аккаунт через /bind.")
        return
    idx = max(0, min(len(profiles) - 1, int(context.user_data.get("progress_index", 0))))
    days = int(context.user_data.get("progress_days", 7))
    days = days if days in {0,7,30} else 7
    player = profiles[idx]
    storage: BindingsStorage = context.application.bot_data["storage"]
    authorized = {account.coc_player_tag for account in
                  storage.get_user_accounts(settings.clan_group_id or 0, update.effective_user.id)}
    if player["tag"] not in authorized:
        await send_or_edit_message(update, "Аккаунт больше не привязан к твоему Telegram.")
        return
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(
                build_url(f"/progress/{encode_tag(player['tag'])}"),
                params={"days": days},
            )
            response.raise_for_status()
            data = response.json()
        count = len(progress_changes(data))
        page = page_index(context.user_data.get("progress_page",0),count,15)
        await send_or_edit_message(
            update,
            progress_text(data, player.get("name") or player["tag"], days, page),
            parse_mode=ParseMode.HTML, reply_markup=progress_keyboard(profiles,idx,days,page,max(1,(count+14)//15)),
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        await send_or_edit_message(update, "Сейчас не удалось получить историю. Попробуй позднее.")


async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if context.args and context.args[0].lower() in {"7","30","all"}:
        context.user_data["progress_days"] = 0 if context.args[0].lower() == "all" else int(context.args[0])
    await show_owned_progress(update, context)


async def growth_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not ensure_private_chat(update):
        return
    await acknowledge(update)
    _,kind,value = callback_data(update).split(":")
    if kind == "page":
        context.user_data["progress_page"] = int(value)
    else:
        days = int(value)
        if days not in {0,7,30}: return
        context.user_data["progress_days"] = days
        context.user_data["progress_page"] = 0
    await show_owned_progress(update, context)


async def cwl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message and not update.callback_query:
        return
    try:
        async with httpx.AsyncClient(timeout=max(45, settings.request_timeout_seconds)) as client:
            payload = await fetch_json(client, "/cwl")
        context.user_data['cwl_snapshot'] = payload
        text, keyboard = cwl_screen(payload)
        await send_or_edit_message(update, text, reply_markup=keyboard)
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning("CWL service unavailable")
        await send_or_edit_message(update, "🏆 Сведения о CWL пока недоступны.", reply_markup=back_buttons("menu_wars"))


async def next_war_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """War or CWL lineup with evidence-labelled categories and server-side permissions."""
    if not update.callback_query and not update.message:
        return
    args = context.args or []
    if args and args[0].lower() in {"war", "cwl"}:
        mode = args[0].lower()
    else:
        mode = context.user_data.get("lineup_mode", "war")
    if mode not in {"war", "cwl"}:
        mode = "war"
    context.user_data["lineup_mode"] = mode
    if len(args) > 1:
        try:
            size = int(args[1])
            if size not in range(5, 51, 5):
                raise ValueError
        except ValueError:
            await send_or_edit_message(update, "Размер КВ: 5–50 с шагом 5. Пример: /nextwar war 30")
            return
        context.user_data["lineup_size"] = size
    params = {"mode": mode}
    if mode == "war" and context.user_data.get("lineup_size"):
        params["size"] = context.user_data["lineup_size"]
    await send_or_edit_message(update, "⏳ Анализирую реальные войны и согласие игроков...",
                                     parse_mode=None)
    try:
        async with httpx.AsyncClient(timeout=max(60, settings.request_timeout_seconds)) as client:
            response = await client.get(build_url("/next-war"), params=params)
            response.raise_for_status()
            data = response.json()
        # The backend intentionally has no access to Telegram identities.
        # Only the bot can show which verified game tags belong to one owner.
        storage: BindingsStorage = context.application.bot_data["storage"]
        all_players = data.get("recommendedLineup") or []
        bindings = storage.get_bindings_for_tags(
            settings.clan_group_id or 0,
            [p["tag"] for p in all_players if p.get("tag")],
        )
        owners: dict[int, list[str]] = {}
        for bound in bindings:
            owners.setdefault(bound.telegram_user_id, []).append(bound.coc_player_tag)
        tag_count = {tag: len(tags) for tags in owners.values() for tag in tags}
        for player in all_players:
            player["ownerAccountCount"] = tag_count.get(player.get("tag"), 1)
        data.setdefault("categories", {})["twinks"] = [
            p for p in all_players if p.get("ownerAccountCount", 1) > 1
        ]
        context.user_data["lineup_snapshot"] = data
        captain = await has_captain_access(update, context)
        stage = context.user_data.pop("lineup_target_stage", "main")
        context.user_data["lineup_stage"] = stage
        context.user_data["lineup_origin"] = (stage, 0)
        if stage in {"kick","promotion","bonus"} and not captain:
            stage = "main"
        await send_or_edit_message(
            update, lineup_message(data, stage), parse_mode=ParseMode.HTML,
            reply_markup=lineup_keyboard(stage, data=data, captain=captain),
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning("Lineup backend unavailable")
        await send_or_edit_message(update, "Не удалось получить состав. Проверь соединение с Clash API.",
                            reply_markup=back_keyboard())
    except Exception:
        logger.exception("Lineup render failed")
        await send_or_edit_message(update, "Ошибка формирования состава. Подробности в журнале аддона.",
                            reply_markup=back_keyboard())


async def lineup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    await acknowledge(update)
    _, stage, page_raw = (callback_data(update) or "").split(":", 2)
    if stage in {"mode", "refresh"}:
        context.user_data["lineup_target_stage"] = context.user_data.get("lineup_stage", "main")
    if stage == "mode":
        if page_raw not in {"war", "cwl"}:
            return
        context.user_data["lineup_mode"] = page_raw
        context.args = []
        await next_war_analysis(update, context)
        return
    if stage == "teamsize":
        size = int(page_raw)
        if size not in range(5, 51, 5):
            return
        context.user_data["lineup_size"] = size
        context.user_data["lineup_mode"] = "war"
        context.args = []
        await next_war_analysis(update, context)
        return
    if stage == "refresh":
        context.args = []
        await next_war_analysis(update, context)
        return
    snapshot = context.user_data.get("lineup_snapshot")
    if not snapshot:
        await send_or_edit_message(update,
            "Сессия устарела. Перезагрузи состав.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Обновить", callback_data="menu_nextwar")]]
            ),
        )
        return
    captain = await has_captain_access(update, context)
    if stage in {"kick", "promotion", "bonus"} and not captain:
        await send_or_edit_message(update,
            "Раздел руководства. Недостаточно прав.",
            reply_markup=back_keyboard(),
        )
        return
    page = max(0, min(50, int(page_raw)))
    if stage == "player":
        snapshot["playerOrigin"] = context.user_data.get("lineup_origin", ("ranking", 0))
    else:
        context.user_data["lineup_origin"] = (stage, page)
        context.user_data["lineup_stage"] = stage
    await send_or_edit_message(update,
        lineup_message(snapshot, stage, page), parse_mode=ParseMode.HTML,
        reply_markup=lineup_keyboard(stage, page, snapshot, captain=captain),
    )


async def cwl_callback(update, context):
    await acknowledge(update)
    data = context.user_data.get('cwl_snapshot')
    if data is None:
        await cwl(update, context)
        return
    _, action, number = callback_data(update).split(':')
    text, keyboard = cwl_screen(data, int(number) if action == 'round' else 0)
    await send_or_edit_message(update, text, reply_markup=keyboard)


async def war_history(update, context):
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            data = await fetch_json(client, '/war-results')
        context.user_data['warlog'] = data.get('wars') or []
        text, keyboard = warlog_screen(context.user_data['warlog'])
        await send_or_edit_message(update, text, reply_markup=keyboard)
    except (httpx.HTTPStatusError, httpx.RequestError):
        await send_or_edit_message(update, 'История временно недоступна. Попробуй позже.', reply_markup=back_buttons('menu_wars'))


async def warlog_callback(update, context):
    await acknowledge(update)
    wars = context.user_data.get('warlog')
    if wars is None:
        await war_history(update, context)
        return
    _, action, value, *extra = callback_data(update).split(':')
    index = int(value)
    if action == 'view' and index < len(wars):
        text, keyboard = war_review(wars[index], index, int(extra[0]) if extra else 0)
    else:
        text, keyboard = warlog_screen(wars, index)
    await send_or_edit_message(update, text, reply_markup=keyboard)


async def fetch_for_operations(path, params=None):
    async with httpx.AsyncClient(timeout=max(60, settings.request_timeout_seconds)) as client:
        response = await client.get(build_url(path), params=params)
        response.raise_for_status()
        return response.json()


async def operations(update, context):
    await context.application.bot_data['operations'].home(update, context)


async def readiness(update, context):
    await context.application.bot_data['operations'].home(update, context, personal=True)


async def poll_list(update, context):
    await context.application.bot_data['polls'].listing(update, context)


async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.application.bot_data["polls"].begin(update, context)


async def clan_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["lineup_target_stage"] = "ranking"
    await next_war_analysis(update, context)


def callback_routes(context):
    return [
        (r'bind_start', bind_start), (r'bind_cancel', bind_cancel),
        (r'pick:(manual|page|player):[0-9]+', roster_callback),
        (r'menu_[a-z]+', menu_callback),
        (r'players:(view|page|hall|donate):[0-9]+|players:(heroes|equipment):[0-9]+:[0-9]+', players_callback),
        (r'acct:(list|view|primary|delete|confirm|progress):[0-9]+|acct:(heroes|equipment):[0-9]+:[0-9]+', account_callback),
        (r'inspect:view:0|inspect:(heroes|equipment):0:[0-9]+', inspect_callback),
        (r'warpage:[0-9]+', warpage_callback),
        (r'growth:days:(0|7|30)|growth:page:[0-9]+', growth_callback),
        (r'staff:(promotion|kick|bonus)', staff_callback),
        (r'lineup:(main|bench|ranking|filters|size|teamsize|details|about|player|refresh|inactive|optout|needs_review|cwl_review|twinks|kick|promotion|bonus|weights):[0-9]+|lineup:mode:(war|cwl)', lineup_callback),
        (r'poll:(done|time|add|remove|publish|cancel|duration:[0-9]+)', context.application.bot_data['polls'].callback),
        (r'poll:list:[0-9]+|poll:view:[0-9]+:[0-9]+', context.application.bot_data['polls'].browse),
        (r'cwl:(table|round):[0-9]+', cwl_callback),
        (r'warlog:list:[0-9]+|warlog:view:[0-9]+:[0-9]+', warlog_callback),
        (r'ops:.*', context.application.bot_data['operations'].callback),
    ]


async def dispatch_callback(update, context):
    await navigator.dispatch(update, context, callback_routes(context), context.application.bot_data['polls'].vote)


async def widget_mode(update, context):
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    widgets = context.application.bot_data.get("widgets")
    if widgets:
        mode = "classic" if update.effective_message.text.split()[0].split("@")[0] == "/classic" else "widgets"
        widgets.store.preference(update.effective_user.id, mode)
    await menu(update, context)


def register_handlers(application):
    """The same complete route registry is used in production and offline UI checks."""
    application.add_handler(TypeHandler(Update, dispatch_widget), group=-3)
    application.add_handler(CommandHandler(["classic", "widgets"], widget_mode))
    application.add_handler(CommandHandler("poll", create_poll))

    application.add_handler(TypeHandler(Update, ignore_duplicate_update), group=-2)
    application.add_handler(MessageHandler(filters.COMMAND, log_any_command), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clan", clan))
    application.add_handler(CommandHandler("topplayers", top_players))
    application.add_handler(CommandHandler("donate", donations))
    application.add_handler(CommandHandler("hall", hall_of_fame))
    application.add_handler(CommandHandler("player", player))
    application.add_handler(CommandHandler("war", war))
    application.add_handler(CommandHandler("nextwar", next_war_analysis))
    application.add_handler(CommandHandler("cwl", cwl))
    application.add_handler(CommandHandler("rating", clan_rating))
    application.add_handler(CommandHandler("ready", readiness))
    application.add_handler(CommandHandler("lineup", operations))
    application.add_handler(CommandHandler("history", war_history))
    application.add_handler(CommandHandler("progress", progress))
    application.add_handler(CommandHandler("captain", captain_report))
    application.add_handler(CommandHandler("staff", staff))
    application.add_handler(CommandHandler("raids", clan_raids))
    application.add_handler(CommandHandler("games", clan_games))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("bind", bind))
    application.add_handler(CommandHandler("unbind", unbind))
    application.add_handler(CommandHandler("accounts", accounts))
    application.add_handler(CommandHandler("primary", primary_account))
    application.add_handler(CommandHandler("unlink", unlink))
    application.add_handler(CommandHandler("adminunlink", admin_unlink))
    application.add_handler(CommandHandler("weights", weights))
    application.add_handler(CommandHandler("setweight", set_weight))
    application.add_handler(CommandHandler("lastseen", last_seen_report))
    application.add_handler(CommandHandler("mytag", mytag))
    application.add_handler(CommandHandler("chatid", chatid))
    application.add_handler(CommandHandler("grouplink", grouplink))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("settings", settings_info))
    application.add_handler(CallbackQueryHandler(dispatch_callback))

    # One router avoids two handlers replying to a successful token message.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, private_text_router),
        group=0,
    )

    # AI mention handler: replies when the bot is mentioned or replied-to (group=2 to run last)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, ai_reply_handler),
        group=2,
    )
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, verify_new_members))
    application.add_error_handler(handle_handler_exception)


async def main() -> None:
    """Main async function to start the bot."""
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
    builder = ApplicationBuilder().token(settings.telegram_bot_token).request(request)
    if settings.widgets_enabled:
        builder = builder.concurrent_updates(WidgetUpdateProcessor())
    application = builder.build()

    application.bot_data["storage"] = BindingsStorage(settings.bindings_db_path)
    application.bot_data["war_outcomes"] = WarAnnouncementStore(settings.bindings_db_path)
    application.bot_data["reminders"] = ReminderStore(settings.bindings_db_path)
    polls = PollController(PollStore(settings.bindings_db_path), settings.clan_group_id,
                           has_captain_access, send_or_edit_message)
    application.bot_data["polls"] = polls
    application.bot_data['operations'] = Operations(
        WarPlanStore(settings.bindings_db_path), application.bot_data['storage'],
        settings.coc_clan_tag, settings.clan_group_id, has_captain_access, send_or_edit_message, fetch_for_operations)
    application.bot_data["widgets"] = Widgets(settings.bindings_db_path, application.bot_data["storage"])
    register_handlers(application)
    application.job_queue.run_repeating(polls.finish_due, interval=30, first=5, name="clan-poll-results")

    if settings.clan_group_id is not None:
        application.job_queue.run_daily(
            sync_tags_daily,
            time=time(hour=9, minute=0, tzinfo=ZoneInfo("Europe/Berlin")),
            name="clan-game-nickname-sync",
        )

    if settings.war_results_enabled and settings.clan_group_id is not None:
        application.job_queue.run_repeating(
            war_result_job,
            interval=timedelta(minutes=3),
            first=timedelta(minutes=2),
            name="completed-war-congratulations",
        )

    if settings.war_reminder_enabled:
        application.job_queue.run_repeating(
            war_reminder_job,
            interval=timedelta(minutes=settings.war_reminder_interval_minutes),
            first=timedelta(minutes=1),
            name="war-reminder",
        )

    logger.info("Telegram bot starting")
    await application.initialize()
    try:
        await sync_bot_commands(application.bot)
    except Exception:
        logger.exception("Could not sync Telegram command menu")
    await application.start()
    await application.updater.start_polling(allowed_updates=list(Update.ALL_TYPES) + ["widget_query"])

    # Register weekly activity report after polling starts
    if settings.lex_coc_tag:
        application.job_queue.run_daily(
            weekly_activity_report_job,
            time=time(hour=10, minute=0, tzinfo=timezone.utc),
            days=(6,),  # 6 = Sunday
            name="weekly-activity-report",
        )

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Telegram bot shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
