"""Compact, HTML-only screens with explicit parents and full pagination."""

from __future__ import annotations

import html
import random
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from app.navigation import bounded_lines, page_index
from app.ui import game_time, format_cwl_message

WELCOME = (
    "Рады видеть тебя в строю, {name}! ⚔️",
    "{name}, добро пожаловать! Для твоих звёзд здесь найдётся место. ⭐",
    "К нам прибыло подкрепление — {name}! 🛡️",
    "{name}, устраивайся. Клан уже готовит следующую войну! 🏰",
)


def welcome_text(mention, name, tag, previous=None):
    choices = [x for x in WELCOME if x != previous]
    phrase = random.choice(choices)
    return (
        phrase.format(name=mention)
        + f"\n🎮 {html.escape(name)} · <code>{html.escape(tag)}</code>\n"
        "Штаб: /menu · Готовность к следующей войне: /ready"
    ), phrase


def home_keyboard(captain=False):
    rows = [
        [
            B("⚔️ Войны", callback_data="menu_wars"),
            B("📋 Состав и готовность", callback_data="menu_ops"),
        ],
        [
            B("📊 Рейтинги", callback_data="menu_ratings"),
            B("👥 Игроки", callback_data="menu_topplayers"),
        ],
        [
            B("👤 Мои аккаунты", callback_data="menu_profile"),
            B("🏰 Жизнь клана", callback_data="menu_community"),
        ],
    ]
    if captain:
        rows.append([B("🛡️ Руководство", callback_data="menu_staff")])
    rows.append([B("❓ Помощь", callback_data="menu_help")])
    return K(rows)


def hub_screen(kind):
    hubs = {
        "wars": (
            "⚔️ Войны клана",
            "Текущая война, семь раундов ЛВК и разбор завершённых боёв.",
            [
                [
                    ("⚔️ Текущая война", "menu_war"),
                    ("🏆 ЛВК · таблица и раунды", "menu_cwl"),
                ],
                [
                    ("📋 Рекомендации состава", "menu_nextwar"),
                    ("📜 История и разбор", "menu_warlog"),
                ],
            ],
        ),
        "ratings": (
            "📊 Рейтинги клана",
            "Выбери режим. Внутри рейтинга можно открыть вклад каждого показателя.",
            [
                [
                    ("⚔️ Рейтинг КВ", "menu_ratingwar"),
                    ("🏆 Рейтинг ЛВК", "menu_ratingcwl"),
                ],
                [("🎁 Донат сезона", "menu_donate"), ("🏅 Зал славы", "menu_hall")],
            ],
        ),
        "community": (
            "🏰 Жизнь клана",
            "Данные клана, рейды и голосования.",
            [
                [("🏰 О клане", "menu_clan"), ("🏛️ Рейды", "menu_raids")],
                [("🗳 Голосования", "menu_polls")],
            ],
        ),
    }
    title, description, rows = hubs[kind]
    return f"<b>{title}</b>\n\n{description}", K(
        [[B(label, callback_data=action) for label, action in row] for row in rows]
        + [[B("◀ Штаб", callback_data="menu_home")]]
    )


def cwl_screen(data, round_number=0):
    if not round_number:
        # Summary contains the standings; round detail is available below.
        text = format_cwl_message({**data, "rounds": []})
    else:
        war = next(
            (
                w
                for i, w in enumerate(data.get("rounds") or [], 1)
                if w.get("roundNumber", i) == round_number
            ),
            None,
        )
        text = f"🏆 <b>ЛВК · раунд {round_number} / 7</b>\n\n"
        if war:
            result = {
                "win": "✅ Победа",
                "loss": "❌ Поражение",
                "draw": "🤝 Ничья",
                "pending": "⏳ Не завершён",
            }
            text += (
                f"Соперник: <b>{html.escape(str(war.get('enemy') or '?'))}</b>\n"
                f"⭐ {war.get('stars', 0)} : {war.get('enemyStars', 0)}\n"
                f"{result.get(war.get('result'), 'Результат уточняется')}\nНачало: {game_time(war.get('startTime'))}"
            )
        else:
            text += "Этот раунд ещё не опубликован или данные пока недоступны. Обнови таблицу позже."
    rows = [
        [
            B(
                str(n) + (" ✓" if n == round_number else ""),
                callback_data=f"cwl:round:{n}",
            )
            for n in range(1, 5)
        ],
        [
            B(
                str(n) + (" ✓" if n == round_number else ""),
                callback_data=f"cwl:round:{n}",
            )
            for n in range(5, 8)
        ],
        [
            B("📊 Таблица", callback_data="cwl:table:0"),
            B("🔄 Обновить", callback_data="menu_cwl"),
        ],
        [
            B("◀ Войны", callback_data="menu_wars"),
            B("🏠 Штаб", callback_data="menu_home"),
        ],
    ]
    return text, K(rows)


def warlog_screen(wars, page=0):
    page = page_index(page, len(wars), 6)
    lines = [
        "📜 <b>История войн</b>",
        "Только завершённые войны, которые бот успел сохранить.",
        "",
    ]
    rows = []
    for index in range(page * 6, min(len(wars), (page + 1) * 6)):
        war = wars[index]
        enemy = str((war.get("opponent") or {}).get("name") or "Соперник")
        title = f"{index + 1}. {enemy[:35]} · {game_time(war.get('startTime'))}"
        lines.append(html.escape(title))
        rows.append([B(title[:60], callback_data=f"warlog:view:{index}:0")])
    if not wars:
        lines.append(
            "История ещё не накоплена. Первый разбор появится после сохранения завершённой войны."
        )
    nav = []
    if page:
        nav.append(B("◀", callback_data=f"warlog:list:{page - 1}"))
    if (page + 1) * 6 < len(wars):
        nav.append(B("▶", callback_data=f"warlog:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("◀ Войны", callback_data="menu_wars")])
    return bounded_lines(lines), K(rows)


def war_review(war, index=0, page=0):
    clan, opponent = war.get("clan") or {}, war.get("opponent") or {}
    members = clan.get("members") or []
    page = page_index(page, len(members), 8)
    allowed = int(war.get("attacksPerMember") or 1)
    enemies = {
        m.get("tag"): m.get("townhallLevel", m.get("townHallLevel"))
        for m in opponent.get("members") or []
    }
    used = sum(min(len(m.get("attacks") or []), allowed) for m in members)
    lines = [
        f"📋 <b>Разбор {'ЛВК' if allowed == 1 else 'КВ'} · {html.escape(str(opponent.get('name') or 'Соперник'))}</b>",
        f"⭐ {clan.get('stars', '?')} : {opponent.get('stars', '?')} · Атаки {used}/{len(members) * allowed}",
        f"{game_time(war.get('startTime'))} · Страница {page + 1}/{max(1, (len(members) + 7) // 8)}",
        "",
    ]
    for member in members[page * 8 : (page + 1) * 8]:
        attacks = member.get("attacks") or []
        own_th = int(member.get("townhallLevel", member.get("townHallLevel", 0)) or 0)
        targets = []
        for attack in attacks[:allowed]:
            th = enemies.get(attack.get("defenderTag"))
            relation = (
                ("равная/выше" if th >= own_th else "ниже")
                if th and own_th
                else "TH неизвестна"
            )
            targets.append(f"{attack.get('stars', 0)}⭐ → TH{th or '?'} ({relation})")
        lines += [
            f"<b>{html.escape(str(member.get('name') or '?')[:40])}</b> · {len(attacks)}/{allowed}",
            "; ".join(targets) if targets else "⚠️ Назначен, атаки не использованы",
        ]
    if not members:
        lines.append("Подробности атак этой войны недоступны.")
    nav = []
    if page:
        nav.append(B("◀", callback_data=f"warlog:view:{index}:{page - 1}"))
    if (page + 1) * 8 < len(members):
        nav.append(B("▶", callback_data=f"warlog:view:{index}:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([B("◀ К истории", callback_data=f"warlog:list:{index // 6}")])
    return bounded_lines(lines), K(rows)
