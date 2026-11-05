"""CoC clan player directory and season donation board."""
from __future__ import annotations

import html
from app.navigation import page_index, back_buttons
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def roster_page(roster: list[dict], page: int = 0, per_page: int = 8) -> str:
    page = max(0, min(page, (len(roster) - 1)//per_page if roster else 0))
    lines = [f"👥 <b>Игроки клана · {len(roster)}</b>",
             f"Страница {page + 1}/{max(1, (len(roster) + per_page - 1)//per_page)}", ""]
    for i in range(page * per_page, min((page+1) * per_page, len(roster))):
        m = roster[i]
        role = {"leader":"👑", "coLeader":"🛡️", "admin":"⭐"}.get(m.get("role"), "👤")
        lines.append(f"{i+1}. {role} <b>{html.escape(str(m.get('name') or '?')[:36])}</b> "
                     f"· TH{m.get('townHallLevel') or '?'} · 🎁 {m.get('donations',0)}")
    lines += ["", "Нажми ник ниже для подробной статистики." if roster else "В списке пока нет игроков. Обнови данные позже."]
    return "\n".join(lines)


def roster_keyboard(roster: list[dict], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    page = page_index(page, len(roster), per_page)
    rows = [[InlineKeyboardButton(str(roster[i].get("name") or "?")[:29],
                                  callback_data=f"players:view:{i}")]
            for i in range(page * per_page, min((page+1)*per_page,len(roster)))]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("◀", callback_data=f"players:page:{page-1}"))
    if (page+1)*per_page < len(roster):
        nav.append(InlineKeyboardButton("▶", callback_data=f"players:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("🎁 Донат", callback_data="players:donate:0"),
         InlineKeyboardButton("🏆 Зал славы", callback_data="players:hall:0")],
        [InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")],
    ])
    return InlineKeyboardMarkup(rows)


def donation_text(members: list[dict], page: int = 0) -> str:
    donors = sorted(members, key=lambda m: (-int(m.get("donations") or 0),
                                            str(m.get("name") or "").lower()))
    lines = ["🎁 <b>Топ доната · текущий сезон</b>", ""]
    page = page_index(page, len(donors), 15)
    lines.append(f"Страница {page+1}/{max(1,(len(donors)+14)//15)}")
    if not donors: lines.append("Игроков для рейтинга пока нет.")
    for i, m in enumerate(donors[page*15:(page+1)*15], page*15+1):
        lines.append(f"{i}. <b>{html.escape(str(m.get('name') or '?')[:32])}</b>"
                     f" · {int(m.get('donations') or 0)} войск")
    lines.append("\nСчётчик сбрасывается каждый сезон. Низкий донат не доказывает неактивность.")
    return "\n".join(lines)


def hall_text(data: dict) -> str:
    lines = ["🏆 <b>Зал славы клана</b>",
             "Раздельные рейтинги по измеряемым данным, не общий балл всех времён.", ""]
    sections = [
        ("🎁 Донат · сезон", "donations", "донат"),
        ("⭐ Качество атак · КВ и ЛВК", "war", "индекс"),
        ("🏛️ Рейды · последний доступный рейд", "raids", "золото"),
        ("📈 На повышение · вклад в клан", "promotion", "индекс"),
    ]
    for title,key,unit in sections:
        lines.append(f"<b>{title}</b>")
        people = data.get(key) or []
        if not people:
            lines.append("История пока недостаточна.")
        for i,m in enumerate(people[:5],1):
            lines.append(f"{i}. {html.escape(str(m.get('name') or '?')[:35])}"
                         f" · {m.get('score','?')} {unit}")
        lines.append("")
    lines.append("🎮 Игры кланов: очки игроков не передаются публичным API, в рейтинг не включены.")
    return "\n".join(lines)


def back_to_roster(page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ К игрокам", callback_data=f"players:page:{page}")],
        [InlineKeyboardButton("🎁 Донат", callback_data="players:donate:0"),
         InlineKeyboardButton("🏆 Зал славы", callback_data="players:hall:0")],
        [InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")],
    ])


def donation_keyboard(count: int, page: int = 0) -> InlineKeyboardMarkup:
    page = page_index(page,count,15)
    rows, nav = [], []
    if page: nav.append(InlineKeyboardButton("◀",callback_data=f"players:donate:{page-1}"))
    if (page+1)*15 < count: nav.append(InlineKeyboardButton("▶",callback_data=f"players:donate:{page+1}"))
    if nav: rows.append(nav)
    rows.extend(back_buttons("menu_ratings").inline_keyboard)
    return InlineKeyboardMarkup(rows)
