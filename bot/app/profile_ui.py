"""Single private CoC player profile for all verified accounts and alt accounts."""
from __future__ import annotations

import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.navigation import bounded_lines, page_index

ROLE_NAMES = {"leader": "Глава", "coLeader": "Соруководитель",
              "admin": "Старейшина", "member": "Участник"}
ROLE_RANKS = {"leader": 4, "coLeader": 3, "admin": 2, "member": 1}


def highest_role(profiles: list[dict]) -> str:
    return max((str(p.get("role") or "member") for p in profiles),
               key=lambda role: ROLE_RANKS.get(role, 0), default="member")


def profile_text(profiles: list[dict], primary_tag: str | None, page: int = 0) -> str:
    role = highest_role(profiles)
    lines = ["👤 <b>Мой профиль · CoC</b>",
             f"🛡️ Максимальное звание: <b>{ROLE_NAMES.get(role, 'Участник')}</b>",
             f"🎮 Подтверждено аккаунтов: <b>{len(profiles)}</b>", ""]
    page = page_index(page, len(profiles), 5)
    lines.append(f"Страница {page + 1}/{max(1, (len(profiles) + 4) // 5)}")
    for i, p in enumerate(profiles[page*5:(page+1)*5], page*5+1):
        title = html.escape(str(p.get("name") or p.get("tag") or "?")[:45])
        mark = "⭐" if p.get("tag") == primary_tag else "▫️"
        role_name = ROLE_NAMES.get(p.get("role"), "Участник")
        pref = {"in": "🟢", "out": "🔴"}.get(p.get("warPreference"), "❔")
        league = html.escape(str(p.get("leagueName") or "Неизвестно")[:38])
        lines += [
            f"{mark} <b>{i}. {title}</b> · TH{p.get('townHallLevel') or '?'}",
            f"   Ур. {p.get('expLevel') or '?'} · {role_name} · {league} · КВ/ЛВК: {pref}",
            f"   🏆 {p.get('trophies','?')} · 🎁 Донат {p.get('donations','?')} · "
            f"⭐ Войны {p.get('warStars','?')}",
        ]
    lines += ["", "Выбери ник ниже для подробной статистики, развития или управления привязкой.",
              "Права главы/соруководителя действуют по любому подтверждённому твинку."]
    return "\n".join(lines)


def profile_keyboard(profiles: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    page = page_index(page, len(profiles), 5)
    rows = [[InlineKeyboardButton(f"🎮 {str(p.get('name') or p.get('tag') or '?')[:26]} · TH{p.get('townHallLevel') or '?'}",
                                  callback_data=f"acct:view:{i}")]
            for i,p in enumerate(profiles[page*5:(page+1)*5], page*5)]
    nav = []
    if page: nav.append(InlineKeyboardButton("◀", callback_data=f"acct:list:{page-1}"))
    if (page+1)*5 < len(profiles): nav.append(InlineKeyboardButton("▶", callback_data=f"acct:list:{page+1}"))
    if nav: rows.append(nav)
    rows.extend([
        [InlineKeyboardButton("📈 Развитие аккаунтов", callback_data="acct:progress:0")],
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="bind_start")],
        [InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")],
    ])
    return InlineKeyboardMarkup(rows)


def detail_text(profile: dict, is_primary: bool = False, section: str = "info", page: int = 0) -> str:
    name = html.escape(str(profile.get("name") or "?")[:55])
    league = html.escape(str(profile.get("leagueName") or "Неизвестна")[:48])
    role = ROLE_NAMES.get(profile.get("role"), "Участник")
    pref = {"in": "🟢 Участвую", "out": "🔴 Отказался"}.get(profile.get("warPreference"), "❔ Неизвестно")
    lines = [f"👤 <b>{name}</b>{' · ⭐ основной' if is_primary else ''}",
             f"Тег: <code>{html.escape(str(profile.get('tag') or ''))}</code>"]
    if profile.get("unavailable"):
        lines.append("⚠️ Профиль временно недоступен. Привязка сохранена; обнови список позже.")
    if section in {"heroes", "equipment"}:
        items = profile.get("heroes" if section == "heroes" else "heroEquipment") or []
        page = page_index(page, len(items), 12)
        lines += ["", "<b>Герои</b>" if section == "heroes" else "<b>Снаряжение</b>",
                  f"Страница {page+1}/{max(1,(len(items)+11)//12)}"]
        for item in items[page*12:(page+1)*12]:
            village = " · база строителя" if item.get("village") == "builderBase" else ""
            lines.append(f"• {html.escape(str(item.get('name') or '?')[:45])}: ур. {item.get('level','?')}{village}")
        if not items: lines.append("API пока не передал данные этого раздела.")
        return bounded_lines(lines)
    lines += [f"🏰 TH{profile.get('townHallLevel') or '?'} · 🛡️ {role}",
              f"📊 Уровень опыта: {profile.get('expLevel') or 'нет данных'}",
              f"🏆 {league} · {profile.get('trophies','?')} кубков", f"🎮 КВ/ЛВК: {pref}",
              f"🎁 Донат: {profile.get('donations','?')} · Получено: {profile.get('donationsReceived','?')}",
              f"⚔️ Военные звёзды за всё время: {profile.get('warStars','?')}",
              f"🎯 Победы атак за сезон: {profile.get('attackWins','?')}", "", "<b>Герои основной деревни</b>"]
    heroes = [h for h in profile.get("heroes") or [] if h.get("village") != "builderBase"]
    for hero in heroes[:8]:
        lines.append(f"• {html.escape(str(hero.get('name') or '?')[:35])}: ур. {hero.get('level','?')}")
    if not heroes: lines.append("Нет данных")
    lines += [f"\nСнаряжение: {len(profile.get('heroEquipment') or [])} предметов. Все уровни — по кнопке ниже.",
              "Опыт — косвенный признак развития. Онлайн и уровни обороны API не передаёт."]
    return bounded_lines(lines)


def detail_keyboard(index: int, is_primary: bool, profile: dict | None = None,
                    section: str = "info", page: int = 0, own: bool = True, inspect: bool = False) -> InlineKeyboardMarkup:
    prefix = "inspect" if inspect else "acct" if own else "players"
    rows = [[InlineKeyboardButton("🦸 Герои", callback_data=f"{prefix}:heroes:{index}:0"),
             InlineKeyboardButton("🛡 Снаряжение", callback_data=f"{prefix}:equipment:{index}:0")]]
    if section != "info":
        items = (profile or {}).get("heroes" if section == "heroes" else "heroEquipment") or []
        page = page_index(page,len(items),12)
        nav = []
        if page: nav.append(InlineKeyboardButton("◀",callback_data=f"{prefix}:{section}:{index}:{page-1}"))
        if (page+1)*12 < len(items): nav.append(InlineKeyboardButton("▶",callback_data=f"{prefix}:{section}:{index}:{page+1}"))
        if nav: rows.append(nav)
        rows.append([InlineKeyboardButton("◀ Карточка",callback_data=f"{prefix}:view:{index}")])
    elif own:
        rows.append([InlineKeyboardButton("📈 Развитие", callback_data=f"acct:progress:{index}")])
        if not is_primary:
            rows.append([InlineKeyboardButton("⭐ Сделать основным", callback_data=f"acct:primary:{index}")])
        rows.append([InlineKeyboardButton("🗑 Удалить привязку", callback_data=f"acct:delete:{index}")])
    rows += [[InlineKeyboardButton("◀ Мои аккаунты" if own else "◀ Игроки клана",
                                   callback_data="menu_topplayers" if inspect else f"acct:list:{index//5}" if own else f"players:page:{index//8}")],
             [InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")]]
    return InlineKeyboardMarkup(rows)


def delete_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Удалить только эту привязку", callback_data=f"acct:confirm:{index}")],
        [InlineKeyboardButton("Отмена", callback_data=f"acct:view:{index}")],
    ])


def progress_keyboard(profiles: list[dict], selected: int, days: int, page: int = 0, pages: int = 1) -> InlineKeyboardMarkup:
    account_page = selected // 5
    rows = [[InlineKeyboardButton(("✓ " if i == selected else "") +
                                  str(p.get("name") or p.get("tag") or "?")[:22],
                                  callback_data=f"acct:progress:{i}")]
            for i,p in enumerate(profiles[account_page*5:(account_page+1)*5], account_page*5)]
    nav = []
    if page: nav.append(InlineKeyboardButton("◀ Изменения", callback_data=f"growth:page:{page-1}"))
    if page+1 < pages: nav.append(InlineKeyboardButton("Изменения ▶", callback_data=f"growth:page:{page+1}"))
    if nav: rows.append(nav)
    rows.append([
        InlineKeyboardButton("7 дней" + (" ✓" if days == 7 else ""), callback_data="growth:days:7"),
        InlineKeyboardButton("30 дней" + (" ✓" if days == 30 else ""), callback_data="growth:days:30"),
        InlineKeyboardButton("Всё" + (" ✓" if days == 0 else ""), callback_data="growth:days:0"),
    ])
    rows.append([InlineKeyboardButton("◀ Профиль", callback_data=f"acct:view:{selected}"),
                 InlineKeyboardButton("🏠 Штаб", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


def progress_changes(data: dict) -> list[str]:
    translations = {"townHallLevel": "Ратуша", "expLevel": "Уровень опыта",
                    "bestTrophies": "Рекорд кубков", "warStars": "Звёзды КВ",
                    "clanCapitalContributions": "Вклад столицы",
                    "attackWins": "Победы атак", "donations": "Донат за сезон"}
    lines = []
    changes = data.get("changes") or {}
    for key,label in translations.items():
        if key in changes:
            item = changes[key]
            if key == "donations" and item["delta"] < 0:
                lines.append("• Донат: сезонный сброс счётчика, не ухудшение прогресса")
            else:
                lines.append(f"• {label}: {item['from']} → {item['to']} ({item['delta']:+g})")
    for key,label in (("heroes","Герои"),("heroEquipment","Снаряжение")):
        for hero, change in changes.get(key, {}).items():
            lines.append(f"• {label}: {html.escape(str(hero)[:35])} {change['from']} → {change['to']}")
    return lines


def progress_text(data: dict, name: str, days: int, page: int = 0) -> str:
    period = "вся история" if days == 0 else f"{days} дней"
    lines = [f"📈 <b>Развитие {html.escape(str(name)[:50])}</b>",
             f"Период: {period} · Последний снимок: {data.get('snapshotDay') or '?'}"]
    if not data.get("historyAvailable"):
        lines.append("Пока только один календарный день наблюдений. Сравнение появится после второго.")
    else:
        changes = progress_changes(data)
        page = page_index(page,len(changes),15)
        lines.append(f"Сравнение с {data.get('comparisonDay')} · Страница {page+1}/{max(1,(len(changes)+14)//15)}:")
        lines.extend(changes[page*15:(page+1)*15])
        if not changes: lines.append("Изменений за выбранный период не обнаружено.")
    lines.append(f"\nВ базе снимков: {data.get('snapshotCount', '?')}. "
                 "CoC API не передаёт таймеры улучшений и полноценные уровни зданий.")
    return bounded_lines(lines)
