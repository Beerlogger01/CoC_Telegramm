"""Private read-only dashboards alongside existing authorized operational flows."""

import asyncio

import httpx
from telegram import Update
from telegram.ext import ApplicationHandlerStop, CallbackContext, BaseUpdateProcessor
from app.settings import settings
from app.widget_store import WidgetStore, StaleWidget
from app.widget_template import WIDGET_HTML, encode_state
from app.widget_views import view


async def api(bot, method, data):
    # PTB 21.4 preserves unknown update fields in api_kwargs. Reuse its HTTP
    # transport and token; no second poller or unofficial endpoint.
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

    async def fetch(self, path, params=None):
        async with httpx.AsyncClient(timeout=6) as client:
            result = await client.get(settings.backend_url + path, params=params)
            result.raise_for_status()
            return result.json()

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
        if self.store.mode(user.id) == "classic":
            return False
        token = self.store.create(user.id)
        try:
            data, actions = await self.data({"s": "home"}, user.id, token, 0)
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
            self.store.publish(token, 0, actions, result["message_id"])
            return True
        except Exception:
            # Unsupported API or transient backend failure: immediately retain classic UX.
            return False

    async def query(self, query, bot, application=None):
        message = "Экран недоступен. Открой /menu."
        try:
            async with asyncio.timeout(10):
                if not settings.widgets_enabled:
                    raise StaleWidget("Widgets выключены. Открой /classic.")
                token, revision, owner, route = self.store.claim(query)
                if route.get("s") == "flow":
                    data, actions = await self.data(
                        {"s": "tools"}, owner, token, revision
                    )
                    self.store.publish(token, revision, actions)
                    await api(
                        bot,
                        "editMessageWidgetData",
                        {
                            "chat_id": owner,
                            "message_id": query["message_id"],
                            "chat_data": data,
                        },
                    )
                    await api(
                        bot,
                        "answerWidgetQuery",
                        {"widget_query_id": query["id"], "data": "Открываю меню ниже…"},
                    )
                    query = {**query, "id": None}
                    if application is not None:
                        from app import bot as handlers

                        routes = {
                            "ready": handlers.readiness,
                            "lineup": handlers.operations,
                            "polls": handlers.poll_list,
                            "staff": handlers.staff,
                            "bind": handlers.bind,
                            "accounts": handlers.accounts,
                        }
                        target = routes[route["flow"]]
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
                        context = CallbackContext.from_update(update, application)
                        context.args = []
                        await target(update, context)
                    return
                try:
                    data, actions = await self.data(route, owner, token, revision)
                except Exception:
                    data = encode_state(
                        {
                            "t": "Данные недоступны",
                            "s": "Повтори запрос или открой /classic",
                            "n": token,
                            "q": revision,
                            "b": [["Повторить", "0"], ["⌂ Штаб", "1"]],
                        }
                    )
                    actions = {"0": route, "1": {"s": "home"}}
                self.store.publish(token, revision, actions)
                await api(
                    bot,
                    "editMessageWidgetData",
                    {
                        "chat_id": owner,
                        "message_id": query["message_id"],
                        "chat_data": data,
                    },
                )
                message = ""
        except StaleWidget as exc:
            message = str(exc)
        except Exception:
            message = "Не удалось обновить. Открой /menu."
            if query.get("id") is None:
                try:
                    await bot.send_message(
                        chat_id=owner,
                        text="Меню временно недоступно. Повтори через /classic.",
                    )
                except Exception:
                    pass
        finally:
            if isinstance(query.get("id"), str):
                try:
                    await api(
                        bot,
                        "answerWidgetQuery",
                        {"widget_query_id": query["id"], "data": message},
                    )
                except Exception:
                    pass  # No tokens, backend payloads or private state in diagnostics.


async def dispatch_widget(update, context):
    query = update.api_kwargs.get("widget_query")
    if not isinstance(query, dict):
        return
    widgets = context.application.bot_data.get("widgets")
    if widgets:
        # Avoid queuing widget requests behind other widget renders. Durable claim
        # invalidates the generation before the first network await.
        context.application.create_task(
            widgets.query(query, context.bot, context.application), update=update
        )
    raise ApplicationHandlerStop


class WidgetUpdateProcessor(BaseUpdateProcessor):
    """Keep legacy handlers sequential, allow widget replies past slow legacy reads."""

    def __init__(self):
        super().__init__(32)
        self.legacy = asyncio.Semaphore(1)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        if isinstance(getattr(update, "api_kwargs", {}).get("widget_query"), dict):
            await coroutine
        else:
            async with self.legacy:
                await coroutine
