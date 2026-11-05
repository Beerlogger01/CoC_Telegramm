"""Persistent per-war readiness and captain-managed rosters. Never starts CoC wars."""

from __future__ import annotations

import html
import json
import sqlite3
import time
from pathlib import Path

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.error import BadRequest, Forbidden, TelegramError
from app.navigation import (
    acknowledge,
    callback_data,
    page_index,
    bounded_lines,
    cancel_input_modes,
)

STATUS = {
    "ready": "✅ Готов",
    "out": "🚫 Не смогу",
    "heroes": "🛠 Герои улучшаются",
    "unknown": "❔ Нет ответа",
}


class PlanConflict(ValueError):
    pass


class WarPlanStore:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS war_plans (
                id INTEGER PRIMARY KEY, clan_tag TEXT NOT NULL, mode TEXT NOT NULL,
                title TEXT NOT NULL, size INTEGER NOT NULL, roster TEXT NOT NULL,
                selected TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
                delivery TEXT NOT NULL DEFAULT 'draft', message_id INTEGER, chat_id INTEGER,
                actor INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS war_readiness (
                plan_id INTEGER NOT NULL REFERENCES war_plans(id), tag TEXT NOT NULL,
                owner INTEGER NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(plan_id,tag));""")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def create(self, clan_tag, mode, title, size, roster, selected, actor):
        if (
            mode not in {"war", "cwl"}
            or size not in range(5, 51, 5)
            or (mode == "cwl" and size != 15)
        ):
            raise ValueError("Неверный размер состава.")
        if not title.strip() or len(title) > 80:
            raise ValueError("Название: от 1 до 80 символов.")
        tags = {p["tag"] for p in roster}
        selected = list(dict.fromkeys(selected))
        if len(selected) > size or not set(selected) <= tags:
            raise ValueError("Неверный состав.")
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE war_plans SET active=0 WHERE clan_tag=? AND mode=?",
                (clan_tag, mode),
            )
            cur = db.execute(
                """INSERT INTO war_plans(clan_tag,mode,title,size,roster,selected,created,expires,actor)
                                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    clan_tag,
                    mode,
                    title,
                    size,
                    json.dumps(roster),
                    json.dumps(selected),
                    now,
                    now + 72 * 3600,
                    actor,
                ),
            )
            return cur.lastrowid

    def get(self, plan_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM war_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if not row:
                return None
            plan = dict(row)
            plan["roster"] = json.loads(plan["roster"])
            plan["selected"] = json.loads(plan["selected"])
            plan["answers"] = {
                row["tag"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM war_readiness WHERE plan_id=?", (plan_id,)
                )
            }
            plan["open"] = bool(plan["active"] and plan["expires"] > time.time())
            return plan

    def current(self, clan_tag):
        with self.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM war_plans WHERE clan_tag=? AND active=1 AND expires>? ORDER BY id DESC",
                    (clan_tag, time.time()),
                )
            ]
        return [self.get(i) for i in ids]

    def answer(self, plan_id, tag, owner, status):
        if status not in STATUS:
            raise ValueError("Неизвестный ответ.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = db.execute(
                "SELECT * FROM war_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if not plan or not plan["active"] or plan["expires"] <= time.time():
                raise PlanConflict("Этот сбор завершён. Открой /ready.")
            if tag not in {p["tag"] for p in json.loads(plan["roster"])}:
                raise ValueError("Аккаунт не входит в список этого сбора.")
            db.execute(
                """INSERT INTO war_readiness VALUES(?,?,?,?,?) ON CONFLICT(plan_id,tag)
                          DO UPDATE SET owner=excluded.owner,status=excluded.status,updated=excluded.updated""",
                (plan_id, tag, owner, status, time.time()),
            )
            # Any changed readiness invalidates a captain's older publication preview.
            db.execute(
                "UPDATE war_plans SET revision=revision+1 WHERE id=?", (plan_id,)
            )

    def change(
        self,
        plan_id,
        revision,
        actor,
        *,
        tag=None,
        unlock=False,
        claim=False,
        close=False,
    ):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM war_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if not row or not row["active"] or row["expires"] <= time.time():
                raise PlanConflict("Сбор завершён. Открой список составов.")
            if row["revision"] != revision:
                raise PlanConflict(
                    "Состав или ответы изменились. Обнови экран перед действием."
                )
            if unlock:
                if row["delivery"] == "sending":
                    raise PlanConflict(
                        "Отправка не подтверждена. Сначала проверь сообщение в группе."
                    )
                db.execute(
                    "UPDATE war_plans SET delivery='draft',revision=revision+1,actor=? WHERE id=?",
                    (actor, plan_id),
                )
                return
            if close:
                db.execute(
                    "UPDATE war_plans SET active=0,revision=revision+1,actor=? WHERE id=?",
                    (actor, plan_id),
                )
                return
            if row["delivery"] != "draft":
                raise PlanConflict(
                    "Состав зафиксирован. Для правок нажми «Редактировать»."
                )
            selected = json.loads(row["selected"])
            if tag:
                if tag not in {p["tag"] for p in json.loads(row["roster"])}:
                    raise ValueError("Игрок не найден.")
                if tag in selected:
                    selected.remove(tag)
                elif len(selected) < row["size"]:
                    selected.append(tag)
                else:
                    raise ValueError(
                        "Состав заполнен. Сначала убери игрока, которого заменяешь."
                    )
                db.execute(
                    "UPDATE war_plans SET selected=?,revision=revision+1,actor=? WHERE id=?",
                    (json.dumps(selected), actor, plan_id),
                )
            elif claim:
                if len(selected) != row["size"]:
                    raise ValueError(
                        f"В составе {len(selected)}/{row['size']}. Сначала заполни состав."
                    )
                db.execute(
                    "UPDATE war_plans SET delivery='sending',revision=revision+1,actor=? WHERE id=?",
                    (actor, plan_id),
                )

    def delivered(self, plan_id, chat_id, message_id):
        with self.connect() as db:
            db.execute(
                "UPDATE war_plans SET delivery='sent',chat_id=?,message_id=? WHERE id=?",
                (chat_id, message_id, plan_id),
            )

    def failed(self, plan_id):
        with self.connect() as db:
            db.execute(
                "UPDATE war_plans SET delivery='draft' WHERE id=? AND delivery='sending'",
                (plan_id,),
            )


def answer_status(plan, tag, storage, group_id):
    answer = plan["answers"].get(tag)
    if not answer or storage.get_user_id_by_tag(group_id, tag) != answer["owner"]:
        return "unknown"
    return answer["status"]


def plan_text(plan, storage, group_id, page=0, all_members=False):
    roster = {p["tag"]: p for p in plan["roster"]}
    chosen = [roster[tag] for tag in plan["selected"] if tag in roster]
    page = page_index(page, len(chosen), 10)
    state = {
        "draft": "Черновик · изменения сохраняются",
        "sending": "Отправка требует проверки в группе",
        "sent": "Зафиксирован и опубликован",
    }[plan["delivery"]]
    ready = sum(
        answer_status(plan, p["tag"], storage, group_id) == "ready" for p in chosen
    )
    lines = [
        f"📋 <b>{html.escape(plan['title'])}</b>",
        f"{'ЛВК' if plan['mode'] == 'cwl' else 'КВ'} · Состав {len(chosen)}/{plan['size']} · Готовы {ready}",
        state,
        "",
    ]
    for i, p in enumerate(
        chosen if all_members else chosen[page * 10 : (page + 1) * 10],
        1 if all_members else page * 10 + 1,
    ):
        status = answer_status(plan, p["tag"], storage, group_id)
        label = (
            {"ready": "✅", "out": "🚫", "heroes": "🛠", "unknown": "❔"}[status]
            if all_members
            else STATUS[status]
        )
        name_limit = 20 if all_members else 35
        lines.append(
            f"{i}. <b>{html.escape(str(p.get('name') or p['tag'])[:name_limit])}</b> · TH{p.get('townHallLevel') or '?'} · {label}"
        )
    if not chosen:
        lines.append("Пока никого нет в составе. Добавь игроков из списка.")
    if all_members:
        lines.append("✅ подтвердил · ❔ ещё не ответил")
    lines.append("\n/ready — подтвердить участие отдельно для каждого своего аккаунта.")
    return bounded_lines(lines, budget=3800)


class Operations:
    def __init__(self, store, storage, clan_tag, group_id, access, send, fetch):
        self.store, self.storage, self.clan_tag, self.group_id = (
            store,
            storage,
            clan_tag,
            group_id,
        )
        self.access, self.send, self.fetch = access, send, fetch

    async def home(self, update, context, personal=False):
        if update.effective_chat.type != "private":
            await self.send(
                update,
                "Составы и ответы по своим аккаунтам доступны в личке бота: /lineup или /ready.",
            )
            return
        plans = self.store.current(self.clan_tag)
        captain = await self.access(update, context)
        lines = [
            "✅ <b>Моя готовность</b>" if personal else "📋 <b>Состав и готовность</b>",
            "Каждый сбор относится к отдельной войне. Ответы по аккаунтам не переносятся в следующий сбор.",
        ]
        rows = []
        for plan in plans:
            rows.append(
                [
                    B(
                        plan["title"][:50],
                        callback_data=f"ops:{'ready' if personal else 'view'}:{plan['id']}:0",
                    )
                ]
            )
        if not plans:
            lines.append(
                "\nСейчас открытых сборов нет. Руководитель может создать сбор из рекомендаций состава."
            )
        if captain:
            rows += [
                [
                    B("⚔️ Новый сбор КВ", callback_data="ops:new:war"),
                    B("🏆 Новый сбор ЛВК", callback_data="ops:new:cwl"),
                ]
            ]
        rows += [
            [B("📊 Рекомендации", callback_data="menu_nextwar")],
            [B("◀ Штаб", callback_data="menu_home")],
        ]
        await self.send(update, "\n".join(lines), reply_markup=K(rows))

    async def title_text(self, update, context):
        mode = context.user_data.get("plan_title")
        if mode not in {"war", "cwl"} or not await self.access(update, context):
            context.user_data.pop("plan_title", None)
            await self.send(update, "Доступ к созданию сбора только у руководства CoC.")
            return
        title = (update.message.text or "").strip()
        if not 1 <= len(title) <= 80:
            await self.send(
                update,
                "Название должно содержать 1–80 символов. Пример: КВ в пятницу, 20:00.",
                reply_markup=K([[B("Отмена", callback_data="menu_ops")]]),
            )
            return
        size = 15 if mode == "cwl" else context.user_data.get("lineup_size", 15)
        try:
            data = await self.fetch("/next-war", {"mode": mode, "size": size})
            roster = data.get("allPlayers") or data.get("recommendedLineup") or []
            selected = [
                p["tag"] for p in (data.get("categories") or {}).get("main", [])
            ][:size]
            # Whitelist only the fields needed by a saved plan.
            roster = [
                {
                    k: p.get(k)
                    for k in (
                        "tag",
                        "name",
                        "townHallLevel",
                        "eligibility",
                        "warPreference",
                    )
                }
                for p in roster
                if p.get("tag")
            ]
            plan_id = self.store.create(
                self.clan_tag,
                mode,
                title,
                size,
                roster,
                selected,
                update.effective_user.id,
            )
        except Exception:
            await self.send(
                update,
                "Не удалось создать сбор: данные состава временно недоступны. Повтори название позже.",
                reply_markup=K([[B("Отмена", callback_data="menu_ops")]]),
            )
            return
        context.user_data.pop("plan_title", None)
        await self.view(update, context, self.store.get(plan_id))

    async def view(self, update, context, plan, page=0, notice=""):
        captain = await self.access(update, context)
        context.user_data["plan_revision"] = (plan["id"], plan["revision"])
        count = len(plan["selected"])
        page = page_index(page, count, 10)
        rows, nav = [], []
        if page:
            nav.append(B("◀", callback_data=f"ops:view:{plan['id']}:{page - 1}"))
        if (page + 1) * 10 < count:
            nav.append(B("▶", callback_data=f"ops:view:{plan['id']}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([B("✅ Мои ответы", callback_data=f"ops:ready:{plan['id']}:0")])
        if captain:
            if plan["delivery"] == "draft":
                rows.append(
                    [
                        B(
                            "✏️ Выбрать игроков",
                            callback_data=f"ops:edit:{plan['id']}:0",
                        ),
                        B(
                            "📨 Зафиксировать и опубликовать",
                            callback_data=f"ops:publish:{plan['id']}",
                        ),
                    ]
                )
            elif plan["delivery"] == "sent":
                rows.append(
                    [
                        B(
                            "✏️ Редактировать состав",
                            callback_data=f"ops:unlock:{plan['id']}",
                        )
                    ]
                )
            rows.append(
                [B("🏁 Закрыть сбор", callback_data=f"ops:closeask:{plan['id']}")]
            )
        rows += [
            [
                B("🔄 Обновить", callback_data=f"ops:view:{plan['id']}:{page}"),
                B("◀ Сборы", callback_data="menu_ops"),
            ]
        ]
        await self.send(
            update,
            (html.escape(notice) + "\n\n" if notice else "")
            + plan_text(plan, self.storage, self.group_id or 0, page),
            reply_markup=K(rows),
        )

    async def editor(self, update, context, plan, page=0, notice=""):
        page = page_index(page, len(plan["roster"]), 8)
        context.user_data["plan_revision"] = (plan["id"], plan["revision"])
        lines = [
            f"✏️ <b>{html.escape(plan['title'])}</b>",
            f"Выбрано {len(plan['selected'])}/{plan['size']} · Страница {page + 1}/{max(1, (len(plan['roster']) + 7) // 8)}",
            html.escape(notice),
        ]
        rows = []
        for i in range(page * 8, min((page + 1) * 8, len(plan["roster"]))):
            p = plan["roster"][i]
            state = STATUS[
                answer_status(plan, p["tag"], self.storage, self.group_id or 0)
            ]
            mark = "✅" if p["tag"] in plan["selected"] else "▫️"
            lines.append(
                f"{mark} {html.escape(str(p.get('name') or p['tag'])[:35])} · {state}"
            )
            rows.append(
                [
                    B(
                        f"{mark} {str(p.get('name') or p['tag'])[:30]}",
                        callback_data=f"ops:toggle:{plan['id']}:{i}",
                    )
                ]
            )
        nav = []
        if page:
            nav.append(B("◀", callback_data=f"ops:edit:{plan['id']}:{page - 1}"))
        if (page + 1) * 8 < len(plan["roster"]):
            nav.append(B("▶", callback_data=f"ops:edit:{plan['id']}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append(
            [B("💾 К сохранённому составу", callback_data=f"ops:view:{plan['id']}:0")]
        )
        await self.send(update, bounded_lines(lines), reply_markup=K(rows))

    async def ready(self, update, context, plan, page=0):
        owned = {
            a.coc_player_tag
            for a in self.storage.get_user_accounts(
                self.group_id or 0, update.effective_user.id
            )
        }
        players = [(i, p) for i, p in enumerate(plan["roster"]) if p["tag"] in owned]
        page = page_index(page, len(players), 4)
        lines = [
            f"✅ <b>{html.escape(plan['title'])}</b>",
            "Ответь за каждый свой аккаунт. Ответ «готов» не отменяет красную отметку CoC.",
            "",
        ]
        rows = []
        for i, p in players[page * 4 : (page + 1) * 4]:
            lines.append(
                f"<b>{html.escape(str(p.get('name') or p['tag']))}</b> · {STATUS[answer_status(plan, p['tag'], self.storage, self.group_id or 0)]}"
            )
            for label, status in (
                (f"{str(p.get('name') or p['tag'])[:16]} · Готов", "ready"),
                ("Не смогу", "out"),
                ("Герои улучшаются", "heroes"),
            ):
                rows.append(
                    [B(label, callback_data=f"ops:answer:{plan['id']}:{i}:{status}")]
                )
        if not players:
            lines.append(
                "В этом сборе нет твоих подтверждённых аккаунтов. Проверь /profile."
            )
        nav = []
        if page:
            nav.append(B("◀", callback_data=f"ops:ready:{plan['id']}:{page - 1}"))
        if (page + 1) * 4 < len(players):
            nav.append(B("▶", callback_data=f"ops:ready:{plan['id']}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([B("◀ Состав", callback_data=f"ops:view:{plan['id']}:0")])
        await self.send(update, bounded_lines(lines), reply_markup=K(rows))

    async def callback(self, update, context):
        await acknowledge(update)
        parts = callback_data(update).split(":")
        action = parts[1]
        if update.effective_chat.type != "private":
            await self.send(update, "Открой /lineup в личке бота.")
            return
        captain = await self.access(update, context)
        if action in {"new", "newsize"}:
            if not captain:
                await self.send(update, "Создавать сборы может руководство CoC.")
                return
            if action == "new" and parts[2] == "war":
                rows = [
                    [
                        B(f"{n} × {n}", callback_data=f"ops:newsize:{n}")
                        for n in range(start, start + 25, 5)
                    ]
                    for start in (5, 30)
                ]
                rows.append([B("Отмена", callback_data="menu_ops")])
                await self.send(
                    update,
                    "⚔️ <b>Новый сбор КВ</b>\nВыбери размер состава.",
                    reply_markup=K(rows),
                )
                return
            mode = "war" if action == "newsize" else parts[2]
            if action == "newsize":
                size = int(parts[2])
                if size not in range(5, 51, 5):
                    return
                context.user_data["lineup_size"] = size
            if mode not in {"war", "cwl"}:
                return
            cancel_input_modes(context)
            context.user_data["plan_title"] = mode
            await self.send(
                update,
                "✍️ Напиши название конкретной войны, например «ЛВК · раунд 4, пятница».\nНовый сбор закроет предыдущий сбор этого режима. Ответы начнутся заново. Срок сбора — 72 часа.\nСостав будет заполнен рекомендациями; потом его можно изменить.",
                reply_markup=K([[B("Отмена", callback_data="menu_ops")]]),
            )
            return
        plan = self.store.get(int(parts[2]))
        if not plan or plan["clan_tag"] != self.clan_tag or not plan["open"]:
            await self.send(
                update,
                "Сбор завершён или не найден.",
                reply_markup=K([[B("◀ Актуальные сборы", callback_data="menu_ops")]]),
            )
            return
        number = int(parts[3]) if len(parts) > 3 else 0
        if action == "view":
            await self.view(update, context, plan, number)
            return
        if action == "ready":
            await self.ready(update, context, plan, number)
            return
        if action == "answer":
            if not 0 <= number < len(plan["roster"]):
                return
            tag = plan["roster"][number]["tag"]
            owned = {
                a.coc_player_tag
                for a in self.storage.get_user_accounts(
                    self.group_id or 0, update.effective_user.id
                )
            }
            if tag not in owned:
                await self.send(update, "Этот аккаунт больше не привязан к тебе.")
                return
            try:
                clan = await self.fetch("/clan")
                if tag not in {m.get("tag") for m in clan.get("memberList") or []}:
                    raise ValueError("Аккаунта больше нет в клане.")
                self.store.answer(plan["id"], tag, update.effective_user.id, parts[4])
            except ValueError as exc:
                await self.send(update, html.escape(str(exc)))
                return
            await self.ready(update, context, self.store.get(plan["id"]))
            return
        if not captain:
            await self.send(update, "Изменять состав может только руководство CoC.")
            return
        if action == "edit":
            await self.editor(update, context, plan, number)
            return
        revision = context.user_data.get("plan_revision")
        if revision is None or revision[0] != plan["id"]:
            await self.view(
                update, context, plan, notice="Обновлён состав. Повтори действие."
            )
            return
        try:
            if action == "toggle":
                if not 0 <= number < len(plan["roster"]):
                    return
                p = plan["roster"][number]
                if (
                    p["tag"] not in plan["selected"]
                    and p.get("eligibility") != "candidate"
                ):
                    raise ValueError(
                        "Игрок не допущен по исходным данным. Создай новый сбор после исправления статуса в игре."
                    )
                self.store.change(
                    plan["id"], revision[1], update.effective_user.id, tag=p["tag"]
                )
                await self.editor(
                    update, context, self.store.get(plan["id"]), number // 8
                )
                return
            if action == "unlock":
                self.store.change(
                    plan["id"], revision[1], update.effective_user.id, unlock=True
                )
            elif action == "closeask":
                await self.send(
                    update,
                    "Закрыть этот сбор? Ответы останутся в истории, новые приниматься не будут.",
                    reply_markup=K(
                        [
                            [B("🏁 Закрыть", callback_data=f"ops:close:{plan['id']}")],
                            [B("Отмена", callback_data=f"ops:view:{plan['id']}:0")],
                        ]
                    ),
                )
                return
            elif action == "close":
                self.store.change(
                    plan["id"], revision[1], update.effective_user.id, close=True
                )
                await self.home(update, context)
                return
            elif action == "publish":
                if not self.group_id:
                    raise ValueError("Группа клана не настроена.")
                # Revalidate real membership, opt-out and manual inactivity immediately before publication.
                current = await self.fetch(
                    "/next-war", {"mode": plan["mode"], "size": plan["size"]}
                )
                players = {
                    p["tag"]: p
                    for p in current.get("allPlayers")
                    or current.get("recommendedLineup")
                    or []
                }
                for tag in plan["selected"]:
                    p = players.get(tag)
                    if not p or p.get("eligibility") != "candidate":
                        raise ValueError(
                            "Состав содержит вышедшего из клана или недопущенного игрока. Обнови состав."
                        )
                    if answer_status(plan, tag, self.storage, self.group_id) in {
                        "out",
                        "heroes",
                    }:
                        raise ValueError(
                            "Кто-то из состава сообщил, что не готов. Сначала замени этого игрока."
                        )
                self.store.change(
                    plan["id"], revision[1], update.effective_user.id, claim=True
                )
                # Persist the claim before Telegram; an ambiguous timeout must never trigger a second send.
                try:
                    publication = plan_text(
                        plan, self.storage, self.group_id, all_members=True
                    ).replace(
                        "Черновик · изменения сохраняются", "Зафиксированный состав"
                    )
                    if plan["message_id"]:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=plan["chat_id"],
                                message_id=plan["message_id"],
                                text=publication,
                                parse_mode="HTML",
                            )
                        except BadRequest as exc:
                            if "message is not modified" not in str(exc).lower():
                                raise
                        message_id = plan["message_id"]
                    else:
                        msg = await context.bot.send_message(
                            chat_id=self.group_id, text=publication, parse_mode="HTML"
                        )
                        message_id = msg.message_id
                    self.store.delivered(plan["id"], self.group_id, message_id)
                except (BadRequest, Forbidden):
                    self.store.failed(plan["id"])
                    raise ValueError(
                        "Telegram отклонил публикацию. Проверь доступ бота к группе."
                    )
                except TelegramError:
                    raise ValueError(
                        "Telegram не подтвердил доставку. Проверь группу: повторная отправка заблокирована, чтобы не создать дубль."
                    )
        except (ValueError, PlanConflict) as exc:
            await self.view(
                update, context, self.store.get(plan["id"]), notice=str(exc)
            )
            return
        await self.view(update, context, self.store.get(plan["id"]))
