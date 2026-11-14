"""Bounded processing, ordered per chat/user; no clan-wide UI lock."""

import asyncio
from collections import OrderedDict
from telegram.ext import BaseUpdateProcessor, CallbackContext


class SessionContext(CallbackContext):
    @property
    def user_data(self):
        data = super().user_data
        if data is None or self._chat_id is None:
            return data
        chats = data.setdefault("_chat_sessions", OrderedDict())
        # Never evict an active wizard automatically. User's Telegram chats are
        # bounded operationally; no shared mutable wizard state across chats.
        return chats.setdefault(self._chat_id, {})


def session_key(update):
    query = getattr(update, "api_kwargs", {}).get("widget_query")
    if isinstance(query, dict):
        return (
            (query.get("chat") or {}).get("id"),
            (query.get("from") or {}).get("id"),
        )
    chat, user = (
        getattr(update, "effective_chat", None),
        getattr(update, "effective_user", None),
    )
    return (getattr(chat, "id", None), getattr(user, "id", None))


class SessionUpdateProcessor(BaseUpdateProcessor):
    def __init__(self):
        super().__init__(64)
        self.sessions = {}
        self.work = asyncio.Semaphore(20)

    async def initialize(self):
        pass

    async def shutdown(self):
        self.sessions.clear()

    async def do_process_update(self, update, coroutine):
        key = session_key(update)
        lock, users = self.sessions.get(key, (asyncio.Lock(), 0))
        self.sessions[key] = (lock, users + 1)
        try:
            async with lock:
                async with self.work:
                    await coroutine
        finally:
            _, users = self.sessions[key]
            if users == 1:
                del self.sessions[key]
            else:
                self.sessions[key] = (lock, users - 1)
