"""Small projections of existing backend data. Never recomputes analytics."""

from urllib.parse import quote


def row_name(value):
    value = str(value or "—")
    raw = value.encode()
    return value if len(raw) <= 32 else raw[:29].decode("utf-8", errors="ignore") + "…"


def short(value, size=22):
    return str(value if value is not None else "—")[:size]


async def view(route, owner, storage, group, fetch):
    page = max(0, int(route.get("p", 0)))
    kind = route.get("s", "home")
    d = {"t": "Штаб клана", "s": "Выбери направление", "k": [], "r": [], "f": ""}
    buttons = []

    def button(label, screen, **kw):
        buttons.append((label, {"s": screen, **kw}))

    def paginate(items, title, size=10):
        nonlocal page
        page = min(page, max(0, (len(items) - 1) // size))
        d["s"] = f"{title} · {page + 1}/{max(1, (len(items) + size - 1) // size)}"
        if page:
            buttons.append(("←", {**route, "p": page - 1}))
        if (page + 1) * size < len(items):
            buttons.append(("→", {**route, "p": page + 1}))
        return items[page * size : (page + 1) * size]

    def player_rows(items, title, metric=None):
        d["l"] = []
        d["_actions"] = []
        for x in paginate(items, title):
            score = x.get(metric) if metric else None
            if isinstance(score, float):
                score = round(score, 1)
            d["l"].append(
                [
                    row_name(x.get("name")),
                    x.get("townHallLevel", x.get("townhallLevel", "—")),
                    score if score is not None else "",
                ]
            )
            d["_actions"].append(
                {"s": "profile", "tag": x["tag"], "back": {**route, "p": page}}
            )
        d["o"] = page * 10
        d["h"] = "TH · " + (
            {"selectionScore": "Балл", "donations": "Донат", "remaining": "Атаки"}.get(
                metric, "Профиль"
            )
        )

    accounts = storage.get_user_accounts(group, owner)
    if kind == "home":
        clan = await fetch("/clan")
        d.update(
            t=short(clan.get("name")),
            s="Твой клан. Твои следующие действия.",
            k=[
                ["Уровень", clan.get("clanLevel", "—")],
                ["Игроки", clan.get("members", "—")],
            ],
        )
        for label, screen in [
            ("⚔ КВ", "war"),
            ("🏆 ЛВК", "cwl"),
            ("👤 Аккаунты", "accounts"),
            ("📊 Рейтинги", "rating"),
            ("👥 Игроки", "players"),
            ("📋 Управление", "tools"),
        ]:
            button(label, screen)
    elif kind in {"accounts", "players"}:
        d["t"] = "Мои аккаунты" if kind == "accounts" else "Игроки клана"
        members = (await fetch("/clan")).get("memberList", [])
        by_tag = {m.get("tag"): m for m in members}
        items = (
            [
                by_tag.get(
                    a.coc_player_tag,
                    {"tag": a.coc_player_tag, "name": a.coc_player_tag},
                )
                for a in accounts
            ]
            if kind == "accounts"
            else members
        )
        player_rows(items, "Аккаунты")
        if kind == "players":
            button("Донат", "donations")
        if not items:
            d["f"] = "Привяжи аккаунт через /bind."
    elif kind in {"profile", "heroes", "equipment", "progress"}:
        tag = route["tag"]
        # Tags come from the server capability map; re-check membership/ownership.
        members = (await fetch("/clan")).get("memberList", [])
        if tag not in {a.coc_player_tag for a in accounts} | {
            x.get("tag") for x in members
        }:
            raise ValueError("Account unavailable")
        player = await fetch("/player/" + quote(tag, safe=""))
        d.update(t=short(player.get("name")), s=short(tag))
        if kind == "profile":
            d["k"] = [
                ["TH", player.get("townHallLevel", "—")],
                ["Уровень", player.get("expLevel", "—")],
                ["Звёзды", player.get("warStars", "—")],
            ]
            d["r"] = [
                [
                    "Донат",
                    "Отдано / получено",
                    f"{player.get('donations', 0)} / {player.get('donationsReceived', 0)}",
                ],
                [
                    "Лига",
                    "",
                    short(
                        (player.get("leagueTier") or player.get("league") or {}).get(
                            "name"
                        )
                    ),
                ],
            ]
            for label, screen in [
                ("Герои", "heroes"),
                ("Снаряжение", "equipment"),
                ("Развитие", "progress"),
            ]:
                button(label, screen, tag=tag)
        elif kind in {"heroes", "equipment"}:
            for x in paginate(
                player.get("heroes" if kind == "heroes" else "heroEquipment", []),
                "Герои" if kind == "heroes" else "Снаряжение",
                size=3,
            ):
                d["r"].append(
                    [
                        short(x.get("name")),
                        "",
                        f"{x.get('level', '—')} / {x.get('maxLevel', '—')}",
                    ]
                )
        else:
            days = route.get("days", 7)
            data = await fetch("/progress/" + quote(tag, safe=""), {"days": days})
            changes = data.get("changes") or {}
            items = []
            for key, x in changes.items():
                if isinstance(x, dict) and "delta" in x:
                    items.append((key, x))
                elif isinstance(x, dict):
                    items.extend(x.items())
            for key, x in paginate(items, "Развитие", size=3):
                d["r"].append(
                    [
                        short(key),
                        "",
                        short(x.get("delta") if isinstance(x, dict) else x),
                    ]
                )
            d["f"] = (
                "Только сохранённые снимки"
                if data.get("historyAvailable")
                else "История пока не накоплена"
            )
            for n in (7, 30, 0):
                button(str(n) + " д." if n else "Всё", "progress", tag=tag, days=n)
        if kind != "profile":
            button("← Профиль", "profile", tag=tag)
    elif kind == "war":
        w = await fetch("/war")
        c = w.get("clan") or {}
        e = w.get("opponent") or {}
        d.update(
            t="⚔ Текущая война",
            s=short(e.get("name") or w.get("state")),
            k=[
                ["Мы", c.get("stars", "—")],
                ["Соперник", e.get("stars", "—")],
                ["Разрушение", str(c.get("destructionPercentage", "—"))[:6]],
            ],
        )
        allowed = int(w.get("attacksPerMember") or 1)
        items = [
            m for m in c.get("members", []) if len(m.get("attacks") or []) < allowed
        ]
        items = [
            {**m, "remaining": allowed - len(m.get("attacks") or [])} for m in items
        ]
        player_rows(items, "Осталось атак", "remaining")
        if not c:
            d["f"] = "Война отсутствует или данные недоступны"
    elif kind == "cwl":
        data = await fetch("/cwl")
        n = route.get("round", 0)
        d.update(
            t="🏆 Лига войн",
            k=[
                ["Место", data.get("currentPlace", "—")],
                ["Звёзды", data.get("totalStars", "—")],
            ],
        )
        if n:
            w = next(
                (x for x in data.get("rounds", []) if x.get("roundNumber") == n), {}
            )
            d["s"] = f"Раунд {n} / 7"
            d["r"] = [
                [
                    short(w.get("enemy") or "Данных пока нет"),
                    short(w.get("result")),
                    f"{w.get('stars', '—')} : {w.get('enemyStars', '—')}",
                ]
            ]
            button("← Раунд", "cwl", round=max(0, n - 1))
            button("Раунд →", "cwl", round=min(7, n + 1))
            button("Таблица", "cwl")
        else:
            d["l"] = [
                [row_name(x.get("name")), x.get("place", "—"), x.get("stars", "—"), ""]
                for x in data.get("standings", [])[:8]
            ]
            d["h"] = "Место · ★"
            d["s"] = "Таблица группы"
            button("Раунды 1–7", "cwl", round=1)
    elif kind == "rating":
        mode = route.get("mode", "war")
        data = await fetch("/next-war", {"mode": mode, "size": 15})
        d["t"] = "Рейтинг " + ("КВ" if mode == "war" else "ЛВК")
        player_rows(data.get("clanRanking", []), "Игроки", "selectionScore")
        button("КВ", "rating", mode="war")
        button("ЛВК", "rating", mode="cwl")
    elif kind == "donations":
        clan = await fetch("/clan")
        d["t"] = "Донат клана"
        player_rows(
            sorted(
                clan.get("memberList", []), key=lambda x: -(x.get("donations") or 0)
            ),
            "Игроки",
            "donations",
        )
    elif kind == "tools":
        d.update(
            t="Управление кланом",
            s="Откроется обычное меню в этом чате",
            f="Права проверяются при каждом действии.",
        )
        for label, flow in [
            ("Готовность", "ready"),
            ("Составы", "lineup"),
            ("Голосования", "polls"),
            ("Руководство", "staff"),
            ("Привязать", "bind"),
            ("Аккаунты", "accounts"),
        ]:
            button(label, "flow", flow=flow)
    else:
        raise ValueError("Unknown screen")
    if route.get("back") and kind == "profile":
        buttons.append(("← К списку", route["back"]))
    if kind != "home":
        button("⌂ Штаб", "home")
    return d, buttons
