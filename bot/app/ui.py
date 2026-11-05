"""Telegram presentation: no API calls, deterministic and unit-testable."""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.navigation import bounded_lines, page_index

BERLIN = ZoneInfo("Europe/Berlin")
COC_DATE = re.compile(r"^\d{8}T\d{6}(?:\.\d+)?Z$")


def parse_game_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = str(raw)
    if not COC_DATE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value.rstrip("Z").split(".")[0], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def game_time(raw: str | None) -> str:
    date = parse_game_time(raw)
    if date is None:
        return "Нет данных"
    return date.astimezone(BERLIN).strftime("%d.%m.%Y, %H:%M")


def remaining_until(raw: str | None, now: datetime | None = None) -> str | None:
    end = parse_game_time(raw)
    if end is None:
        return None
    now = now or datetime.now(timezone.utc)
    seconds = int((end - now).total_seconds())
    if seconds <= 0:
        return "Время истекло"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин"


def safe_tag_name(name: str, max_chars: int = 16) -> str:
    """Telegram regular-member tag; remove emoji, control chars and truncate."""
    text = unicodedata.normalize("NFKC", name or "")
    clean = []
    for ch in text:
        code = ord(ch)
        if unicodedata.category(ch) in {"So", "Cs", "Cf", "Cc"} or 0x1F000 <= code <= 0x1FAFF:
            continue
        clean.append(ch)
    return " ".join("".join(clean).split())[:max_chars]


def normalize_name(name: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(name)).casefold() if ch.isalnum())


def format_war_message(war: dict, page: int = 0) -> str:
    labels = {"inWar": "⚔️ Идёт", "preparation": "🛡 Подготовка",
              "warEnded": "🏁 Завершена", "notInWar": "Нет активной войны"}
    state = war.get("state", "unknown")
    clan, other = war.get("clan") or {}, war.get("opponent") or {}
    name = html.escape(str(clan.get("name") or "Наш клан"))
    enemy = html.escape(str(other.get("name") or "Соперник"))
    lines = ["⚔️ <b>Война клана</b>", f"Статус: <b>{labels.get(state, 'Неизвестен')}</b>"]
    if state == "notInWar":
        return "\n".join(lines)
    size = war.get("teamSize")
    if size:
        lines.append(f"Участники: {size} × {size}")
    lines.extend(["", f"🏰 <b>{name}</b>  vs  <b>{enemy}</b>"])
    if state in {"inWar", "warEnded"}:
        lines.append(f"⭐ {clan.get('stars', 0)} : {other.get('stars', 0)}")
        lines.append(f"💥 {float(clan.get('destructionPercentage') or 0):.1f}% : {float(other.get('destructionPercentage') or 0):.1f}%")
        attacks_per = int(war.get("attacksPerMember") or 1)
        total = int(size or 0) * attacks_per
        if total:
            used = sum(len(m.get("attacks") or []) for m in clan.get("members", []))
            lines.append(f"🎯 Наши атаки: {used}/{total} · осталось {max(0, total - used)}")
            missing = [
                (m.get("name") or "Игрок", max(0, attacks_per - len(m.get("attacks") or [])))
                for m in clan.get("members", []) if len(m.get("attacks") or []) < attacks_per
            ]
            if missing:
                page = page_index(page,len(missing),10)
                lines.extend(["", "<b>Неиспользованные атаки:</b>" if state == "warEnded" else "<b>Кому ещё атаковать:</b>"])
                for name, left in missing[page*10:(page+1)*10]:
                    lines.append(f"• {html.escape(str(name))} · {left} атак")
                if len(missing) > 10:
                    lines.append(f"Страница {page+1}/{(len(missing)+9)//10} · Всего игроков: {len(missing)}")
    if state == "preparation":
        lines.append(f"Начало: {game_time(war.get('startTime'))}")
    else:
        lines.append(f"Начало: {game_time(war.get('startTime'))}")
        lines.append(f"Конец: {game_time(war.get('endTime'))}")
    if state in {"inWar", "preparation"}:
        countdown = remaining_until(war.get("endTime") if state == "inWar" else war.get("startTime"))
        if countdown:
            lines.append(f"⏳ Осталось: {countdown}")
    return "\n".join(lines)


def war_keyboard(war: dict, page: int = 0) -> InlineKeyboardMarkup:
    allowed = int(war.get("attacksPerMember") or 1)
    count = sum(len(m.get("attacks") or []) < allowed for m in (war.get("clan") or {}).get("members") or [])
    page = page_index(page,count,10)
    rows, nav = [], []
    if war.get("state") in {"inWar","warEnded"}:
        if page: nav.append(InlineKeyboardButton("◀ Игроки",callback_data=f"warpage:{page-1}"))
        if (page+1)*10 < count: nav.append(InlineKeyboardButton("Игроки ▶",callback_data=f"warpage:{page+1}"))
    if nav: rows.append(nav)
    rows += [[InlineKeyboardButton("🔄 Обновить",callback_data="menu_war")],
             [InlineKeyboardButton("◀ Войны",callback_data="menu_wars"),InlineKeyboardButton("🏠 Штаб",callback_data="menu_home")]]
    return InlineKeyboardMarkup(rows)


def format_raid_message(data: dict) -> str:
    raid = data.get("currentRaid")
    if not raid:
        return "🏛 <b>Рейды столицы</b>\nСведения о последнем рейде отсутствуют."
    state = raid.get("state")
    state_label = {"ongoing": "⚔️ Идёт", "ended": "🏁 Последний рейд завершён",
                   "notInWar": "Нет активного рейда"}.get(state, str(state or "Неизвестно"))
    lines = ["🏛 <b>Рейды столицы</b>", f"Статус: {html.escape(state_label)}",
             f"Начало: {game_time(raid.get('startTime'))}",
             f"Конец: {game_time(raid.get('endTime'))}"]
    if state == "ongoing":
        countdown = remaining_until(raid.get("endTime"))
        if countdown:
            lines.append(f"⏳ Осталось: {countdown}")
    for key, label in (("capitalTotalLoot", "Добыто золота"), ("raidsCompleted", "Районов разгромлено"),
                       ("totalAttacks", "Атак использовано"), ("defensiveReward", "Награда за защиту")):
        if raid.get(key) is not None:
            lines.append(f"{label}: {raid[key]}")
    members = sorted(raid.get("members") or [], key=lambda x: x.get("capitalResourcesLooted") or 0, reverse=True)
    if members:
        lines.extend(["", "<b>Топ добытчиков:</b>"])
        for m in members[:5]:
            attacks = m.get("attacks", 0)
            lines.append(f"• {html.escape(str(m.get('name', '?')))}: {m.get('capitalResourcesLooted', 0)} золота · {attacks} атак")
    return "\n".join(lines)


def lineup_keyboard(stage: str = "main", page: int = 0, data: dict | None = None,
                    captain: bool = False) -> InlineKeyboardMarkup:
    data = data or {}
    mode = data.get("mode", "war")
    rows = [[InlineKeyboardButton("⚔️ КВ" + (" ✓" if mode == "war" else ""), callback_data="lineup:mode:war"),
             InlineKeyboardButton("🏆 ЛВК" + (" ✓" if mode == "cwl" else ""), callback_data="lineup:mode:cwl")]]
    members = data.get("recommendedLineup") or []
    categories = data.get("categories") or {}
    indices = {p.get("tag"):i for i,p in enumerate(members)}
    subset = data.get("clanRanking") or [] if stage == "ranking" else categories.get(stage) or []
    if stage == "details": subset = members
    per_page = 8 if stage in {"kick", "inactive", "cwl_review", "details"} else 15
    if stage == "player":
        origin, origin_page = data.get("playerOrigin", ("details", page//8))
        rows.append([InlineKeyboardButton("◀ К списку", callback_data=f"lineup:{origin}:{origin_page}")])
    elif subset:
        page = page_index(page,len(subset),per_page)
        for position,p in enumerate(subset[page*per_page:(page+1)*per_page], page*per_page+1):
            if p.get("tag") in indices:
                rows.append([InlineKeyboardButton(f"{position}. {str(p.get('name') or '?')[:25]} · TH{p.get('townHallLevel') or '?'}",
                                                  callback_data=f"lineup:player:{indices[p.get('tag')]}")])
        nav = []
        if page: nav.append(InlineKeyboardButton("◀",callback_data=f"lineup:{stage}:{page-1}"))
        if (page+1)*per_page < len(subset): nav.append(InlineKeyboardButton("▶",callback_data=f"lineup:{stage}:{page+1}"))
        if nav: rows.append(nav)
    if stage == "size" and mode == "war":
        for start in (5,30):
            rows.append([InlineKeyboardButton(f"{n} × {n}",callback_data=f"lineup:teamsize:{n}") for n in range(start,start+25,5)])
    if stage == "filters":
        for left,right in ((('🕒 48+ ч','inactive'),('🚫 Отказ','optout')), (('❔ Уточнить','needs_review'),('👥 Твинки','twinks'))):
            rows.append([InlineKeyboardButton(label,callback_data=f"lineup:{key}:0") for label,key in (left,right)])
        if mode == "cwl": rows.append([InlineKeyboardButton("⚠️ Пропуски ЛВК · уточнить",callback_data="lineup:cwl_review:0")])
        if captain:
            rows += [[InlineKeyboardButton("⚠️ Дисциплина",callback_data="lineup:kick:0"), InlineKeyboardButton("📈 Повышение",callback_data="lineup:promotion:0")],
                     [InlineKeyboardButton("🎖 Бонусы ЛВК",callback_data="lineup:bonus:0")]]
    rows += [[InlineKeyboardButton("✅ Состав",callback_data="lineup:main:0"),InlineKeyboardButton("🪑 Запас",callback_data="lineup:bench:0")],
             [InlineKeyboardButton("📊 Рейтинг",callback_data="lineup:ranking:0"),InlineKeyboardButton("🔎 Категории",callback_data="lineup:filters:0")],
             [InlineKeyboardButton("📊 Веса",callback_data="lineup:weights:0"),InlineKeyboardButton("ℹ️ Методика",callback_data="lineup:about:0")]]
    if mode == "war": rows.append([InlineKeyboardButton("⚙️ Размер КВ",callback_data="lineup:size:0")])
    rows += [[InlineKeyboardButton("🔄 Обновить",callback_data="lineup:refresh:0")],
             [InlineKeyboardButton("◀ Рейтинги" if stage == "ranking" else "◀ Войны",callback_data="menu_ratings" if stage == "ranking" else "menu_wars"),InlineKeyboardButton("🏠 Штаб",callback_data="menu_home")]]
    return InlineKeyboardMarkup(rows)


def lineup_message(data: dict, stage: str = "main", page: int = 0) -> str:
    members = data.get("recommendedLineup") or []
    categories = data.get("categories") or {}
    size = max(0, min(50, int(data.get("recommendedSize") or 0)))
    if not categories:
        size = max(1, size) if members else 0
        categories = {"main": members[:size], "bench": members[size:]}
    total = int(data.get("observedWarCount") or 0)
    mode = "ЛВК" if data.get("mode") == "cwl" else "КВ"
    header = (
        f"⚔️ <b>Следующая война · {html.escape(str(data.get('clanName') or 'Клан')[:50])}</b>\n"
        f"Режим: <b>{mode}</b> · Состав: {len(categories.get('main') or [])}/{data.get('lineupSize') or size} · "
        f"История: {total} войн\n"
    )
    if stage == "filters":
        return header + "\n🔎 <b>Категории игроков</b>\nВыбери причину или рекомендацию. Пустая категория означает, что подходящих игроков сейчас нет."
    if stage == "size":
        return header + "\nВыбери размер обычной КВ. В ЛВК состав всегда 15 × 15."
    if stage == "weights":
        weights = data.get("weights") or {
            "strength": 20, "discipline": 35, "quality": 35, "role": 10,
        }
        labels = [("strength", "🏰 Сила TH и прокачка"),
                  ("discipline", "🎯 Дисциплина и пропуски"),
                  ("quality", "⭐ Качество атак с учётом силы цели"),
                  ("role", "🛡️ Звание в CoC")]
        if "strength" not in weights:
            # Backwards-compatible display for old fixtures.
            labels = [("combat", "🏰 Сила"),("reliability", "🎯 Атаки"),
                      ("performance", "⭐ Звёзды"),("participation", "✅ Заявка"),
                      ("activity", "🕒 Отметка")]
        lines = [header, "📊 <b>Веса и приоритеты</b>"]
        lines.extend(f"{name}: <b>{float(weights.get(key, 0)):g}%</b>" for key,name in labels)
        lines += ["", "Сумма: 100%. Неизвестные атаки не считаются пропуском автоматически.",
                  "Согласие зелёное/красное из CoC — строгое условие для обоих режимов.",
                  "Руководителям: /setweight war quality 40 или /setweight cwl quality 40"]
        return "\n".join(lines)
    if stage == "about":
        return header + (
            "\n<b>Жёсткие правила допуска</b>\n"
            "1. Красная отметка в CoC → НЕ включать в КВ или ЛВК.\n"
            "2. Пропуск ЛВК → предупреждение и проверка готовности; решение за руководством.\n"
            "3. Подтверждённая вручную неактивность более 48 ч → отдельный резерв.\n"
            "4. Пропущены три назначенные войны подряд → кандидат на кик, НЕ автоматический кик.\n\n"
            "<b>Рейтинг (после допуска):</b> отдельно настраиваются дисциплина, "
            "качество атаки с поправкой на ратушу цели, сила и звание.\n"
            "КВ: относительная прокачка среди той же TH. ЛВК: 80% сила самой TH "
            "(100 × (TH / максимальная TH)³), 20% относительная прокачка. "
            "За максимум берётся не ниже TH18.\n"
            "Прокачка: 55% герои основной деревни, 15% уровень опыта, "
            "20% снаряжение, 10% лига. Неизвестные сигналы исключаются; без сверстников — 50.\n"
            "Уровень опыта — косвенный признак развития, не точная оценка обороны: "
            "уровни зданий API не передаёт. Лига и звание — дополнительные сигналы.\n"
            "ЛВК всегда 15 × 15; размер КВ можно выбрать.\n"
            "⚠️ Последний вход НЕ передаётся CoC API. Ручная отметка руководства отдельно "
            "от реальной API-статистики. Все дисциплинарные решения принимает человек."
        )
    if stage == "player":
        if not 0 <= page < len(members):
            return header + "\nИгрок не найден."
        m = members[page]
        hist = m.get("history") or {}
        cwl = m.get("cwlHistory") or {}
        pref = {"in": "🟢 Согласен", "out": "🔴 Отказ", "unknown": "❔ Неизвестно"}.get(
            m.get("warPreference"), "❔ Неизвестно")
        age = m.get("lastSeenHoursAgo")
        last = f"По отметке руководства {age / 24:.1f} дн. назад" if age is not None else "Неизвестно (API не передаёт)"
        score = m.get("selectionScore")
        rank = f"{score:.1f}" if isinstance(score, (int,float)) else "н/д"
        role = {"leader": "Глава", "coLeader": "Соруководитель",
                "admin": "Старейшина", "member": "Участник"}.get(m.get("role"), "Неизвестно")
        confidence = {"high":"высокая", "medium":"средняя", "low":"низкая"}.get(m.get("confidence"), "низкая")
        rows = [
            header,
            f"<b>👤 {html.escape(str(m.get('name') or '?')[:50])}</b> · TH{m.get('townHallLevel') or '?'}",
            f"Тег: <code>{html.escape(str(m.get('tag') or ''))}</code>",
            f"🛡️ {role} · Лига: {html.escape(str(m.get('league') or '?')[:40])}",
            f"Уровень опыта: {m.get('expLevel') or 'нет данных'} · Место в рейтинге: {m.get('clanRank', 'н/д')}",
            f"Индекс {mode}: {rank} · Достоверность: {confidence}",
            f"Относительная прокачка: {m.get('relativeStrength','н/д')} · "
            f"Сравнимых TH: {m.get('townHallPeers','н/д')}",
            f"Сила для {mode}: {m.get('combatStrength', 'н/д')}",
            f"Участие в КВ/ЛВК (CoC): {pref}",
            f"Последний вход: {last}",
            f"Обычные КВ: {hist.get('wars', 0)} войн · {hist.get('attacksUsed', 0)}/{hist.get('attacksPossible', 0)} атак",
            f"Качество атак (равные/сильнее): {hist.get('threeStarsEqualOrHigher', 0)} триплов",
            f"ЛВК: {cwl.get('wars', 0)} раундов · {cwl.get('attacksUsed', 0)}/{cwl.get('attacksPossible', 0)} атак",
            f"Пропусков войн подряд: {m.get('missedWarsInARow', 0)}",
            f"Донат: {m.get('donations', 0)} · Рейд-золото: {m.get('raidLoot') if m.get('raidLoot') is not None else 'нет данных'}",
        ]
        rows.append("\n<b>Из чего сложился балл</b>")
        labels = {"strength": "Сила", "discipline": "Дисциплина", "quality": "Качество атак", "role": "Звание"}
        for key, value in (m.get("scoreComponents") or {}).items():
            weight = (m.get("scoreWeights") or data.get("weights") or {}).get(key, 0)
            rows.append(f"• {labels.get(key, key)}: {value:.1f} × {weight:g}% = {value * weight / 100:.1f}")
        rows.append("Мало истории — оценка приближена к нейтральной, а не к нулю. Звание даёт небольшой бонус.")
        rows.extend("• " + html.escape(str(x)[:180]) for x in (m.get("reasons") or [])[:4])
        return "\n".join(rows)
    if stage == "details":
        return header + "\nВыбери игрока. Каждая запись содержит свою статистику КВ/ЛВК."
    titles = {
        "main": "✅ Рекомендуемый состав", "bench": "🪑 Запасные",
        "ranking": "📊 Рейтинг всего клана",
        "inactive": "🕒 По ручной отметке не заходили 48+ часов",
        "optout": "🚫 Красная отметка: отказались от КВ/ЛВК",
        "needs_review": "❔ Нет достоверного игрового согласия",
        "cwl_review": "⚠️ Пропуски этой ЛВК · уточнить причину",
        "twinks": "👥 Привязанные дополнительные аккаунты (информация)",
        "kick": "⚠️ На рассмотрение, НЕ автоматический кик",
        "promotion": "📈 Кандидаты на повышение по вкладу в клан",
        "bonus": "🎖 Кандидаты на бонус ЛВК",
    }
    subset = (data.get("clanRanking") or []) if stage == "ranking" else categories.get(stage) or []
    per_page = 8 if stage in {"kick", "inactive", "cwl_review"} else 15
    offset = page_index(page, len(subset), per_page) * per_page
    rows = [header, f"<b>{titles.get(stage,'Рейтинг')}</b> · {len(subset)}"]
    if not subset:
        empty = {"main": "Пока нет допущенных игроков. Проверь согласие в CoC и категории.",
                 "bench": "Все допущенные игроки уже поместились в состав.",
                 "optout": "Красных отметок среди полученных профилей нет.",
                 "needs_review": "Согласие получено для всех доступных профилей.",
                 "cwl_review": "В наблюдаемых завершённых раундах пропусков не найдено.",
                 "twinks": "Подтверждённых дополнительных аккаунтов пока нет.",
                 "kick": "Игроков, требующих проверки дисциплины по этим критериям, нет."}
        rows.append(empty.get(stage, "Пока нет подтверждённых кандидатов либо ещё недостаточно истории."))
        if stage == "inactive":
            rows.append("Список определяется по ручной отметке руководителя, НЕ по API.")
        if stage == "bonus":
            rows.append("Нужны завершённые раунды ЛВК с сохранёнными атаками. Пустой список не ошибка.")
    for i, p in enumerate(subset[offset:offset + per_page], offset + 1):
        label = html.escape(str(p.get("name") or "?")[:35])
        rank = p.get("selectionScore")
        display = f"{rank:.1f}" if isinstance(rank, (int,float)) else "н/д"
        hist = (p.get("cwlHistory") if data.get("mode") == "cwl" else p.get("history")) or {}
        observed = f"{hist['wars']} войн" if hist.get("wars") else "без истории"
        rows.append(f"{i}. <b>{label}</b> · TH{p.get('townHallLevel') or '?'} · {display} · {observed}")
        if stage == "ranking":
            state = {"candidate": "допущен", "optout": "отказ", "inactive": "48+ ч",
                     "cwl_review": "пропуск ЛВК", "needs_review": "уточнить"}.get(p.get("eligibility"), "уточнить")
            rows.append(f"   Ур. {p.get('expLevel') or '?'} · {state}")
        if stage == "promotion":
            contrib = p.get("contributionScore") or {}
            rows.append(f"   ↳ Вклад: {contrib.get('score','н/д')} · донат: {p.get('donations',0)}"
                        f" · рейды: {p.get('raidLoot') if p.get('raidLoot') is not None else 'н/д'}")
        if stage in {"kick","inactive","cwl_review"}:
            rows.extend("   ↳ " + html.escape(str(x)[:120]) for x in (p.get("reasons") or [])[:2])
    if len(subset) > per_page:
        rows.append(f"Страница {offset // per_page + 1}/{(len(subset) + per_page - 1) // per_page}")
    rows.append("\nℹ️ Только рекомендации руководству, не автоматические действия.")
    return bounded_lines(rows)


def format_cwl_message(data: dict) -> str:
    rounds = data.get("rounds") or []
    if data.get("state") == "notInWar" and not rounds:
        return "🏆 <b>Лига клановых войн</b>\nСейчас нет доступных раундов CWL."
    lines = ["🏆 <b>Лига клановых войн · CWL</b>",
             f"Раундов в группе: {data.get('scheduledRounds', '?')}",
             f"Доступно наших войн: {data.get('observedRounds', 0)} / 7",
             f"⭐ Всего наших звёзд: {data.get('totalStars', 0)} "
             f"(атаки {data.get('attackStars', 0)} + бонус {data.get('winBonus', 0)})",
             f"Место сейчас по общим звёздам: {data.get('currentPlace') or 'нет данных'}" +
             (" (предварительно, неполные данные)" if not data.get('standingsComplete') else ""),
             f"✅ {data.get('wins', 0)} побед · 🤝 {data.get('draws', 0)} ничьих · ❌ {data.get('losses', 0)} поражений",
             ""]
    table = data.get("standings") or []
    if table:
        lines.append("<b>Таблица по общим звёздам</b>")
        for row in table:
            lines.append(f"{row['place']}. {html.escape(str(row['name'])[:40])} · ⭐ {row['stars']} · Σ {row.get('destruction', 0):.1f}%")
        lines.append("Звёзды атак + 10 за завершённую победу. При равенстве — суммарное разрушение; полный паритет даёт общее место.")
        if not data.get("standingsComplete"):
            lines.append(f"⚠️ Неполные данные результатов или разрушения. Не загружено войн: {data.get('missingWars', '?')}. Место предварительное.")
        lines.append("")
    results = {"win": "✅ Победа", "draw": "🤝 Ничья", "loss": "❌ Поражение", "pending": "⏳ Не завершена"}
    for index, war in enumerate(rounds[-10:], start=max(1, len(rounds) - 9)):
        lines.append(f"<b>{war.get('roundNumber', index)}. {html.escape(str(war.get('enemy') or '?'))}</b> · {results.get(war.get('result'), '⏳')}")
        lines.append(f"⭐ {war.get('stars', 0)} : {war.get('enemyStars', 0)} · {game_time(war.get('startTime'))}")
    lines.append("\nℹ️ Расчёт по наблюдаемым звёздам — не официальная таблица мест CWL. Незавершённые войны могут изменить место.")
    return bounded_lines(lines)
