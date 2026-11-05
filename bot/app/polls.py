"""Durable clan votes: one changeable ballot per Telegram user, timed results."""
from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError, Forbidden
from app.navigation import callback_data, acknowledge, page_index, back_buttons, cancel_input_modes

logger = logging.getLogger(__name__)


class PollStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS clan_polls (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL, question TEXT NOT NULL,
                options TEXT NOT NULL, closes_at REAL NOT NULL,
                message_id INTEGER, announced INTEGER NOT NULL DEFAULT 0
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS clan_votes (
                poll_id INTEGER NOT NULL REFERENCES clan_polls(id),
                user_id INTEGER NOT NULL, option_index INTEGER NOT NULL,
                PRIMARY KEY(poll_id,user_id)
            )""")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def create(self, chat_id: int, creator_id: int, question: str,
               options: list[str], closes_at: float) -> int:
        if not question.strip() or len(question) > 300:
            raise ValueError("Вопрос: от 1 до 300 символов.")
        if (not 2 <= len(options) <= 10 or any(not x.strip() or len(x) > 80 for x in options)
                or len({x.strip().casefold() for x in options}) != len(options)):
            raise ValueError("Нужно 2–10 разных вариантов, до 80 символов каждый.")
        with self.connect() as conn:
            cur = conn.execute("""INSERT INTO clan_polls(chat_id,creator_id,question,options,closes_at)
                                   VALUES(?,?,?,?,?)""",
                               (chat_id, creator_id, question, json.dumps(options, ensure_ascii=False), closes_at))
            return cur.lastrowid

    def attach(self, poll_id: int, message_id: int):
        with self.connect() as conn:
            conn.execute("UPDATE clan_polls SET message_id=? WHERE id=?", (message_id, poll_id))

    def get(self, poll_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clan_polls WHERE id=?", (poll_id,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["options"] = json.loads(out["options"])
            counts = dict(conn.execute("""SELECT option_index,COUNT(*) FROM clan_votes
                                          WHERE poll_id=? GROUP BY option_index""", (poll_id,)))
        out["counts"] = [counts.get(i, 0) for i in range(len(out["options"]))]
        return out

    def vote(self, poll_id: int, chat_id: int, message_id: int, user_id: int,
             option_index: int, now: float | None = None) -> str:
        with self.connect() as conn:
            # Serialize the deadline check with the ballot update.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM clan_polls WHERE id=?", (poll_id,)).fetchone()
            if not row or row["chat_id"] != chat_id or row["message_id"] != message_id:
                return "Голосование не найдено в этой группе."
            if row["closes_at"] <= (time.time() if now is None else now):
                return "Голосование уже завершено."
            options = json.loads(row["options"])
            if not 0 <= option_index < len(options):
                return "Нет такого варианта."
            conn.execute("""INSERT INTO clan_votes VALUES(?,?,?)
                            ON CONFLICT(poll_id,user_id) DO UPDATE SET option_index=excluded.option_index""",
                         (poll_id, user_id, option_index))
        return f"Голос принят: {options[option_index]}"

    def due(self, now: float | None = None) -> list[dict]:
        with self.connect() as conn:
            ids = [row[0] for row in conn.execute("""SELECT id FROM clan_polls
                WHERE message_id IS NOT NULL AND announced=0 AND closes_at<=? ORDER BY id LIMIT 50""",
                                                (time.time() if now is None else now,))]
        return [self.get(poll_id) for poll_id in ids]

    def listing(self, chat_id: int) -> list[dict]:
        with self.connect() as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM clan_polls WHERE chat_id=? AND message_id IS NOT NULL ORDER BY id DESC LIMIT 100", (chat_id,))]
        return [self.get(i) for i in ids]

    def claim_announcement(self, poll_id: int) -> bool:
        # Commit BEFORE Telegram. A timeout may mean delivery succeeded.
        with self.connect() as conn:
            return bool(conn.execute("UPDATE clan_polls SET announced=2 WHERE id=? AND announced=0", (poll_id,)).rowcount)

    def release_announcement(self, poll_id: int):
        with self.connect() as conn:
            conn.execute("UPDATE clan_polls SET announced=0 WHERE id=? AND announced=2", (poll_id,))

    def mark_announced(self, poll_id: int):
        with self.connect() as conn:
            conn.execute("UPDATE clan_polls SET announced=1 WHERE id=?", (poll_id,))


def poll_text(poll: dict, *, result: bool = False) -> str:
    lines = ["🗳 <b>Итоги голосования</b>" if result else "🗳 <b>Голосование клана</b>",
             html.escape(poll["question"]), ""]
    total = sum(poll.get("counts") or [])
    for i, option in enumerate(poll["options"]):
        suffix = f" — {poll['counts'][i]} ({100 * poll['counts'][i] / total:.0f}%)" if result and total else (" — 0" if result else "")
        lines.append(f"{i + 1}. {html.escape(option)}{suffix}")
    if result:
        lines.append(f"\nВсего голосов: {total}.")
        if total:
            most = max(poll["counts"])
            winners = [html.escape(x) for x, count in zip(poll["options"], poll["counts"]) if count == most]
            lines.append(("Победил вариант: " if len(winners) == 1 else "Равенство голосов: ") + "; ".join(winners))
        else:
            lines.append("Никто не проголосовал.")
    else:
        deadline = datetime.fromtimestamp(poll["closes_at"], timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        lines += [f"\nДо {deadline}.", "Один голос на участника Telegram. До окончания выбор можно изменить."]
    return "\n".join(lines)


def vote_keyboard(poll: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(option, callback_data=f"vote:{poll['id']}:{i}")]
                                 for i, option in enumerate(poll["options"])])


def duration_seconds(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(м|мин|m|ч|час|h|д|дн|d)?\s*", text.lower())
    if not match:
        raise ValueError("Напиши срок: 30м, 2ч или 3д (число без единицы — минуты).")
    unit = match[2] or "м"
    value = int(match[1]) * (3600 if unit in {"ч", "час", "h"} else 86400 if unit in {"д", "дн", "d"} else 60)
    if not 60 <= value <= 30 * 86400:
        raise ValueError("Срок: от 1 минуты до 30 дней.")
    return value


class PollController:
    def __init__(self, store: PollStore, group_id: int | None, has_access, send):
        self.store, self.group_id, self.has_access, self.send = store, group_id, has_access, send

    async def begin(self, update, context):
        if not await self.has_access(update, context):
            await self.send(update, "Создать голосование может глава или соруководитель CoC в личке бота.")
            return
        if not self.group_id:
            await self.send(update, "Сначала настрой группу клана (CLAN_GROUP_ID).")
            return
        if context.user_data.get("awaiting_tag") or context.user_data.get("awaiting_player_token"):
            await self.send(update, "Заверши или отмени привязку аккаунта, затем открой /poll.")
            return
        cancel_input_modes(context)
        context.user_data["poll_draft"] = {"stage": "question", "options": []}
        await self.show(update, context)

    async def show(self, update, context, notice: str = ""):
        draft = context.user_data.get("poll_draft")
        if not draft:
            await self.send(update, "Черновик не найден. Начни заново: /poll.")
            return
        rows = []
        lines = ["🗳 <b>Новое голосование</b>"]
        if notice:
            lines.append(html.escape(notice))
        if draft.get("question"):
            lines += [html.escape(draft["question"])]
        lines += [f"{i + 1}. {html.escape(x)}" for i, x in enumerate(draft["options"])]
        stage = draft["stage"]
        if stage == "question":
            lines.append("\nНапиши вопрос (до 300 символов).")
        elif stage == "options":
            lines.append("\nНапиши следующий вариант (до 80 символов). Нужно 2–10 вариантов.")
            rows.append([InlineKeyboardButton("✅ Варианты готовы", callback_data="poll:done")])
            if draft["options"]:
                rows.append([InlineKeyboardButton("↶ Удалить последний вариант", callback_data="poll:remove")])
        elif stage == "duration":
            lines.append("\nВыбери длительность или напиши свою: 30м, 2ч, 3д.")
            for values in ((("1 мин", 60), ("1 ч", 3600), ("6 ч", 21600)),
                           (("24 ч", 86400), ("3 дня", 259200), ("7 дней", 604800))):
                rows.append([InlineKeyboardButton(label, callback_data=f"poll:duration:{value}") for label, value in values])
        elif stage == "preview":
            lines.append(f"\nДлительность: {draft['duration'] // 60} мин. После публикации срок начнётся заново.")
            lines.append("Голосование появится в настроенной группе клана; итоги придут туда автоматически.")
            rows += [[InlineKeyboardButton("📨 Опубликовать в группе", callback_data="poll:publish")],
                     [InlineKeyboardButton("➕ Ещё вариант", callback_data="poll:add"),
                      InlineKeyboardButton("🕒 Изменить срок", callback_data="poll:time")]]
        rows.append([InlineKeyboardButton("Отмена", callback_data="poll:cancel")])
        await self.send(update, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

    async def text(self, update, context):
        draft = context.user_data.get("poll_draft")
        if not draft or not update.message:
            return
        text = (update.message.text or "").strip()
        try:
            if draft["stage"] == "question":
                if not text or len(text) > 300:
                    raise ValueError("Вопрос: от 1 до 300 символов.")
                draft.update(question=text, stage="options")
            elif draft["stage"] == "options":
                if len(draft["options"]) >= 10:
                    raise ValueError("Уже 10 вариантов. Нажми «Варианты готовы».")
                if not text or len(text) > 80:
                    raise ValueError("Вариант: от 1 до 80 символов.")
                if text.casefold() in {x.casefold() for x in draft["options"]}:
                    raise ValueError("Такой вариант уже есть.")
                draft["options"].append(text)
            elif draft["stage"] == "duration":
                draft.update(duration=duration_seconds(text), stage="preview")
            else:
                raise ValueError("Используй кнопки предпросмотра.")
        except ValueError as exc:
            await self.show(update, context, str(exc))
            return
        await self.show(update, context)

    async def callback(self, update, context):
        await acknowledge(update)
        if not await self.has_access(update, context):
            await self.send(update, "Доступ только руководству CoC в личке.")
            return
        action = callback_data(update).split(":")[1]
        if action == "cancel":
            context.user_data.pop("poll_draft", None)
            await self.listing(update, context)
            return
        draft = context.user_data.get("poll_draft")
        if not draft:
            await self.show(update, context)
            return
        if action in {"done", "time"}:
            if len(draft["options"]) < 2:
                await self.show(update, context, "Добавь минимум два варианта.")
                return
            draft["stage"] = "duration"
        elif action == "remove" and draft["stage"] == "options":
            if draft["options"]: draft["options"].pop()
        elif action == "add":
            draft["stage"] = "options"
        elif action == "duration" and len(draft["options"]) >= 2:
            seconds = int(callback_data(update).split(":")[2])
            if not 60 <= seconds <= 30 * 86400:
                return
            draft.update(duration=seconds, stage="preview")
        elif action == "publish" and draft["stage"] == "preview":
            # Pop before the first await to prevent double publication by double taps.
            draft = context.user_data.pop("poll_draft")
            poll_id = self.store.create(self.group_id, update.effective_user.id, draft["question"],
                                        draft["options"], time.time() + draft["duration"])
            poll = self.store.get(poll_id)
            try:
                message = await context.bot.send_message(chat_id=self.group_id, text=poll_text(poll),
                                                         parse_mode=ParseMode.HTML, reply_markup=vote_keyboard(poll))
            except (BadRequest, Forbidden):
                logger.warning("Telegram rejected clan poll %s", poll_id)
                context.user_data["poll_draft"] = draft
                await self.show(update, context, "Telegram отклонил публикацию. Проверь доступ бота к группе и повтори.")
                return
            except TelegramError:
                logger.warning("Delivery of clan poll %s is uncertain; no automatic resend", poll_id)
                await self.send(update, "Telegram не подтвердил доставку. Проверь группу перед созданием нового голосования: повторная отправка отключена, чтобы не создать дубль.", reply_markup=back_buttons("menu_polls"))
                return
            self.store.attach(poll_id, message.message_id)
            await self.send(update, "✅ Голосование опубликовано в группе клана. Итоги появятся после окончания срока.")
            return
        await self.show(update, context)

    async def vote(self, update, context):
        query = update.callback_query
        if not query.message or not update.effective_user:
            await query.answer("Открой исходное сообщение в группе.")
            return
        _, poll_id, index = callback_data(update).split(":")
        answer = self.store.vote(int(poll_id), query.message.chat.id, query.message.message_id,
                                 update.effective_user.id, int(index))
        await query.answer(answer, show_alert=True)

    async def listing(self, update, context, page=0):
        polls = self.store.listing(self.group_id or 0)
        page = page_index(page,len(polls),6)
        rows = []
        lines = ["🗳 <b>Голосования клана</b>",f"Страница {page+1}/{max(1,(len(polls)+5)//6)}"]
        for poll in polls[page*6:(page+1)*6]:
            status = "🟢" if poll["closes_at"] > time.time() else "🏁"
            rows.append([InlineKeyboardButton(f"{status} {poll['question'][:45]}",callback_data=f"poll:view:{poll['id']}:{page}")])
        if not polls: lines.append("Голосований пока нет. Руководство может создать первое.")
        nav = []
        if page: nav.append(InlineKeyboardButton("◀",callback_data=f"poll:list:{page-1}"))
        if (page+1)*6 < len(polls): nav.append(InlineKeyboardButton("▶",callback_data=f"poll:list:{page+1}"))
        if nav: rows.append(nav)
        if await self.has_access(update,context):
            rows.append([InlineKeyboardButton("➕ Создать голосование",callback_data="menu_poll")])
        rows.extend(back_buttons("menu_community").inline_keyboard)
        await self.send(update,"\n".join(lines),reply_markup=InlineKeyboardMarkup(rows))

    async def browse(self, update, context):
        await acknowledge(update)
        _,action,value,*extra = callback_data(update).split(":")
        if action == "list":
            await self.listing(update,context,int(value)); return
        poll = self.store.get(int(value))
        if not poll or poll["chat_id"] != self.group_id or not poll["message_id"]:
            await self.listing(update,context); return
        ended = poll["closes_at"] <= time.time()
        text = poll_text(poll,result=ended)
        if not ended: text += "\n\nГолосуй в исходном сообщении группы клана."
        rows = []
        if str(self.group_id).startswith("-100"):
            rows.append([InlineKeyboardButton("↗ Открыть в группе",url=f"https://t.me/c/{str(self.group_id)[4:]}/{poll['message_id']}")])
        rows.extend(back_buttons(f"poll:list:{int(extra[0]) if extra else 0}").inline_keyboard)
        await self.send(update,text,reply_markup=InlineKeyboardMarkup(rows))

    async def finish_due(self, context):
        for poll in self.store.due():
            try:
                await context.bot.edit_message_text(chat_id=poll["chat_id"], message_id=poll["message_id"],
                                                    text=poll_text(poll, result=True), parse_mode=ParseMode.HTML,
                                                    reply_markup=None)
            except BadRequest as exc:
                reason = str(exc).lower()
                if not any(x in reason for x in ("message is not modified", "message to edit not found", "message can't be edited")):
                    logger.warning("Could not render poll result %s", poll["id"])
                    continue
            except TelegramError:
                # Retrying an edit cannot duplicate a message.
                logger.warning("Could not update poll %s; retrying next cycle", poll["id"])
                continue
            if not self.store.claim_announcement(poll["id"]):
                continue
            try:
                await context.bot.send_message(chat_id=poll["chat_id"], text=poll_text(poll, result=True), parse_mode=ParseMode.HTML)
            except (BadRequest, Forbidden):
                self.store.release_announcement(poll["id"])
                logger.warning("Telegram rejected poll result %s", poll["id"])
                continue
            except TelegramError:
                logger.warning("Poll result %s delivery uncertain; result stays in original message; no resend", poll["id"])
                continue
            self.store.mark_announced(poll["id"])
