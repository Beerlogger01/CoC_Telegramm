"""One editable screen per user/chat; callback capabilities expire on navigation."""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from html.parser import HTMLParser

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

_answered: ContextVar[bool] = ContextVar("callback_answered", default=False)

_action: ContextVar[str | None] = ContextVar("callback_action", default=None)


def callback_data(update) -> str:
    return (
        _action.get()
        or (update.callback_query.data if update.callback_query else "")
        or ""
    )


def page_index(page: int, count: int, size: int) -> int:
    return max(0, min(int(page), max(0, (count - 1) // size)))


def back_buttons(parent="menu_home", label="◀ Назад") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=parent)]]
    if parent != "menu_home":
        rows.append([InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(text: str) -> str:
    parser = _PlainText()
    parser.feed(text)
    return "".join(parser.parts)


def bounded_lines(lines: list[str], budget: int = 3600) -> str:
    """Keep whole HTML lines, never slice tags or escaped names."""
    out, length = [], 0
    for line in lines:
        width = len(plain_text(line).encode("utf-16-le")) // 2 + 1
        if length + width > budget:
            out.append("… Продолжение доступно на следующих страницах.")
            break
        out.append(line)
        length += width
    return "\n".join(out)


@dataclass
class Screen:
    token: str
    chat_id: int
    message: object
    expires: float
    actions: set[str]


class Navigator:
    def __init__(self, ttl=1800, capacity=2000):
        self.ttl, self.capacity = ttl, capacity
        self.screens: OrderedDict[int, Screen] = OrderedDict()

    def reset(self):
        self.screens.clear()

    async def render(self, update, text, parse_mode=ParseMode.HTML, reply_markup=None):
        if not update.effective_user or not update.effective_chat:
            return None
        owner, chat_id = update.effective_user.id, update.effective_chat.id
        old = self.screens.get(owner)
        target = (
            update.callback_query.message
            if update.callback_query
            else (old.message if old and old.chat_id == chat_id else None)
        )
        token = secrets.token_hex(4)
        actions = set()
        rows = []
        for row in (reply_markup or back_buttons()).inline_keyboard:
            buttons = []
            for button in row:
                if button.callback_data:
                    action = button.callback_data
                    actions.add(action)
                    buttons.append(
                        InlineKeyboardButton(
                            button.text, callback_data=f"ui:{token}:{action}"
                        )
                    )
                else:
                    buttons.append(button)
            rows.append(buttons)
        markup = InlineKeyboardMarkup(rows)
        # All main screens use escaped HTML; legacy plain messages remain plain.
        if (
            parse_mode == ParseMode.HTML
            and len(plain_text(text).encode("utf-16-le")) // 2 > 3900
        ):
            text = bounded_lines(text.splitlines())
        kwargs = dict(text=text, parse_mode=parse_mode, reply_markup=markup)
        try:
            if target:
                message = await target.edit_text(**kwargs)
            else:
                message = await update.effective_message.reply_text(**kwargs)
        except BadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                # Keep the new callback token usable even when Telegram declines a text edit.
                try:
                    message = await target.edit_reply_markup(reply_markup=markup)
                except BadRequest as markup_error:
                    if "message is not modified" not in str(markup_error).lower():
                        raise
                    message = target
            elif "parse entities" in reason or "unsupported start tag" in reason:
                kwargs.update(text=plain_text(text), parse_mode=None)
                message = (
                    await target.edit_text(**kwargs)
                    if target
                    else await update.effective_message.reply_text(**kwargs)
                )
            elif target and (
                "message to edit not found" in reason
                or "message can't be edited" in reason
            ):
                message = await update.effective_message.reply_text(**kwargs)
            else:
                raise
        if not hasattr(message, "message_id"):
            message = target
        self.screens[owner] = Screen(
            token, chat_id, message, time.monotonic() + self.ttl, actions
        )
        self.screens.move_to_end(owner)
        while len(self.screens) > self.capacity:
            self.screens.popitem(last=False)
        return message

    async def dispatch(self, update, context, routes, vote_handler):
        query = update.callback_query
        raw = query.data or ""
        # Published votes have their own durable chat/message/deadline validation.
        if re.fullmatch(r"vote:[0-9]+:[0-9]+", raw):
            await vote_handler(update, context)
            return
        parts = raw.split(":", 2)
        screen = self.screens.get(update.effective_user.id)
        valid = (
            len(parts) == 3
            and parts[0] == "ui"
            and screen is not None
            and query.message
            and getattr(query.message, "is_accessible", True)
            and parts[1] == screen.token
            and time.monotonic() < screen.expires
            and query.message.chat.id == screen.chat_id
            and screen.message is not None
            and query.message.message_id == screen.message.message_id
            and parts[2] in screen.actions
        )
        if not valid:
            try:
                await query.answer(
                    "Эта кнопка устарела или открыта другим игроком. Открой своё меню: /menu.",
                    show_alert=True,
                )
            except BadRequest:
                pass
            return
        action = parts[2]
        # Consume before awaiting: a duplicate tap cannot repeat a mutation.
        screen.expires = 0
        marker = _action.set(action)
        ack_marker = _answered.set(False)
        try:
            for pattern, handler in routes:
                if re.fullmatch(pattern, action):
                    await handler(update, context)
                    return
            await query.answer("Раздел обновился. Открой /menu.", show_alert=True)
        finally:
            _action.reset(marker)
            _answered.reset(ack_marker)


navigator = Navigator()


async def acknowledge(update):
    if update.callback_query and (not _action.get() or not _answered.get()):
        try:
            await update.callback_query.answer()
            _answered.set(True)
        except BadRequest as exc:
            if (
                "query is too old" not in str(exc).lower()
                and "query id is invalid" not in str(exc).lower()
            ):
                raise


async def ignore_duplicate_update(update, context):
    """Telegram retries must not repeat commands, ballots or join greetings."""
    seen = context.application.bot_data.setdefault("recent_update_ids", OrderedDict())
    if update.update_id in seen:
        raise ApplicationHandlerStop
    seen[update.update_id] = None
    while len(seen) > 2000:
        seen.popitem(last=False)


def cancel_input_modes(context):
    for key in (
        "awaiting_tag",
        "awaiting_player_token",
        "pending_tag",
        "pending_coc_tag",
        "poll_draft",
        "plan_title",
    ):
        context.user_data.pop(key, None)
