"""Private Widgets with recoverable delivery and secret-free diagnostics."""

import asyncio
import logging
import sqlite3
import httpx
from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram.ext import ApplicationHandlerStop
from app.settings import settings
from app.sessions import SessionContext, SessionUpdateProcessor
from app.widget_backend import WidgetBackend
from app.widget_store import WidgetStore, StaleWidget
from app.widget_template import WIDGET_HTML, encode_state
from app.widget_views import view

logger = logging.getLogger(__name__)
# Compatibility import for downstream code; production always uses this processor.
WidgetUpdateProcessor = SessionUpdateProcessor


def diagnostic(exc):
    if isinstance(exc, PermissionError) or isinstance(exc, Forbidden):
        return "PERMISSION_DENIED"
    if isinstance(exc, StaleWidget):
        return str(exc) if str(exc).startswith("WIDGET_") else "WIDGET_EXPIRED"
    if isinstance(exc, sqlite3.OperationalError):
        return "SQLITE_BUSY"
    if isinstance(exc, TimeoutError) or isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT"
    if isinstance(exc, httpx.HTTPError):
        return "BACKEND_UNAVAILABLE"
    if isinstance(exc, BadRequest):
        reason = str(exc).lower()
        if any(
            x in reason
            for x in (
                "method not found",
                "unknown method",
                "unsupported",
                "not supported",
            )
        ):
            return "TELEGRAM_API_UNSUPPORTED"
        return "TELEGRAM_API_REJECTED"
    if isinstance(exc, NetworkError):
        return "TELEGRAM_API_UNAVAILABLE"
    if isinstance(exc, ValueError):
        return "STATE_ENCODING_ERROR"
    return "WIDGET_INTERNAL_ERROR"


def record(stage, exc):
    code = diagnostic(exc)
    logger.warning("widget operation=%s code=%s", stage, code)
    return code


async def api(bot, method, data):
    async with asyncio.timeout(3):
        return await bot._post(
            method,
            data,
            read_timeout=2,
            write_timeout=2,
            connect_timeout=2,
            pool_timeout=1,
        )


class Widgets:
    def __init__(self, path, bindings):
        self.store = WidgetStore(path)
        self.bindings = bindings
        self.backend = WidgetBackend()

    async def close(self):
        await self.backend.close()

    async def db(self, method, *args):
        # SQLite waits never occupy the event loop; transient locks get bounded retries.
        for attempt in range(3):
            try:
                return await asyncio.to_thread(getattr(self.store, method), *args)
            except sqlite3.OperationalError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))

    async def fetch(self, path, params=None):
        return await self.backend.fetch(path, params)

    async def data(self, route, owner, token, revision):
        async with asyncio.timeout(7):
            d, buttons = await view(
                route, owner, self.bindings, settings.clan_group_id or 0, self.fetch
            )
        actions = {str(i): r for i, (_, r) in enumerate(buttons)}
        d.update(
            n=token,
            q=revision,
            b=[[label, str(i)] for i, (label, _) in enumerate(buttons)],
        )
        # Interactive row capabilities follow navigation actions; tags stay server-side.
        row_routes = d.pop("_actions", [])
        for row, route in zip(d.get("l", []), row_routes):
            action = str(len(actions))
            actions[action] = route
            row.append(action)
        return encode_state(d), actions

    async def open(self, update, context):
        chat, user = update.effective_chat, update.effective_user
        if (
            not settings.widgets_enabled
            or not chat
            or chat.type != "private"
            or not user
            or chat.id != user.id
        ):
            return False
        stage = "storage"
        try:
            if await self.db("mode", user.id) == "classic":
                return False
            token = await self.db("create", user.id)
            stage = "backend"
            data, actions = await self.data({"s": "home"}, user.id, token, 0)
            stage = "sendWidget"
            result = await api(
                context.bot,
                "sendWidget",
                {
                    "chat_id": chat.id,
                    "html": WIDGET_HTML,
                    "chat_data": data,
                    "public_data": "",
                    "allow_queries": True,
                    "caption": "Штаб клана · /classic — обычное меню · /widgets — новая панель",
                },
            )
            stage = "storage"
            await self.db("publish", token, 0, actions, result["message_id"])
            return True
        except Exception as exc:
            record(stage, exc)
            return False

    async def query(self, query, bot, application=None):
        message = ""
        token = None
        flow = None
        stage = "storage"
        try:
            async with asyncio.timeout(10):
                if not settings.widgets_enabled:
                    raise StaleWidget("WIDGET_DISABLED")
                token, revision, owner, route, pending = await self.db("begin", query)
                if pending is None:
                    stage = "backend"
                    try:
                        data, actions = await self.data(
                            {"s": "tools"} if route.get("s") == "flow" else route,
                            owner,
                            token,
                            revision,
                        )
                    except Exception as exc:
                        code = record(stage, exc)
                        data = encode_state(
                            {
                                "t": "Временно недоступно",
                                "s": code,
                                "n": token,
                                "q": revision,
                                "b": [["Повторить", "0"], ["Штаб", "1"]],
                            }
                        )
                        actions = {"0": route, "1": {"s": "home"}}
                        # A failed view must never launch an operational flow.
                        route = {
                            **route,
                            "s": route["s"] if route["s"] != "flow" else "tools",
                        }
                    pending = await self.db(
                        "prepare", token, revision, route, data, actions
                    )
                stage = "editMessageWidgetData"
                try:
                    await api(
                        bot,
                        "editMessageWidgetData",
                        {
                            "chat_id": owner,
                            "message_id": query["message_id"],
                            "chat_data": pending["data"],
                        },
                    )
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
                stage = "storage"
                changed = await self.db(
                    "delivered", token, revision, pending["actions"]
                )
                if changed and pending["route"].get("s") == "flow":
                    flow = pending["route"]["flow"]
        except Exception as exc:
            code = record(stage, exc)
            message = code + " · Повтори нажатие; /classic — обычное меню."
            if token:
                try:
                    await self.db("release", token)
                except Exception as failure:
                    record("storage", failure)
        finally:
            if isinstance(query.get("id"), str):
                try:
                    await api(
                        bot,
                        "answerWidgetQuery",
                        {"widget_query_id": query["id"], "data": message},
                    )
                except Exception as exc:
                    record("answerWidgetQuery", exc)
        if flow and application:
            # Execute on the same chat/user lane, AFTER timely widget acknowledgement.
            try:
                from app import bot as handlers

                routes = {
                    "ready": handlers.readiness,
                    "lineup": handlers.operations,
                    "polls": handlers.poll_list,
                    "staff": handlers.staff,
                    "bind": handlers.bind,
                    "accounts": handlers.accounts,
                }
                update = Update.de_json(
                    {
                        "update_id": 0,
                        "message": {
                            "message_id": query["message_id"],
                            "date": 0,
                            "chat": query["chat"],
                            "from": query["from"],
                            "text": "/menu",
                        },
                    },
                    bot,
                )
                context = SessionContext.from_update(update, application)
                context.args = []
                await routes[flow](update, context)
            except Exception as exc:
                record("flow", exc)
                await bot.send_message(
                    chat_id=query["chat"]["id"],
                    text="Меню временно недоступно. Открой /classic.",
                )


async def dispatch_widget(update, context):
    query = update.api_kwargs.get("widget_query")
    if not isinstance(query, dict):
        return
    widgets = context.application.bot_data.get("widgets")
    if widgets:
        await widgets.query(query, context.bot, context.application)
    raise ApplicationHandlerStop
