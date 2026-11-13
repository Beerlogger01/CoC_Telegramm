"""Small projections of existing backend data. Never recomputes analytics."""

from urllib.parse import quote


def short(value, size=22):
    return str(value if value is not None else "—")[:size]


async def view(route, owner, storage, group, fetch):
    page = max(0, int(route.get("p", 0)))
    kind = route.get("s", "home")
    d = {"t": "Штаб клана", "s": "Выбери направление", "k": [], "r": [], "f": ""}
    buttons = []

    def button(label, screen, **kw):
        buttons.append((label, {"s": screen, **kw}))

    def paginate(items, title):
        nonlocal page
        page = min(page, max(0, (len(items) - 1) // 3))
        d["s"] = f"{title} · {page + 1}/{max(1, (len(items) + 2) // 3)}"
        if page:
            buttons.append(("←", {**route, "p": page - 1}))
        if (page + 1) * 3 < len(items):
            buttons.append(("→", {**route, "p": page + 1}))
        return items[page * 3 : (page + 1) * 3]

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
        items = (
            [{"tag": a.coc_player_tag, "name": a.coc_player_tag} for a in accounts]
            if kind == "accounts"
            else (await fetch("/clan")).get("memberList", [])
        )
        for x in paginate(items, "Аккаунты"):
            button(short(x.get("name")), "profile", tag=x["tag"])
            d["r"].append(
                [
                    short(x.get("name")),
                    short(x.get("tag")),
                    "TH " + str(x.get("townHallLevel", "—")),
                ]
            )
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
            for key, x in paginate(items, "Развитие"):
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
        for m in paginate(items, "Неиспользованные атаки"):
            d["r"].append(
                [
                    short(m.get("name")),
                    "Осталось атак",
                    str(allowed - len(m.get("attacks") or [])),
                ]
            )
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
            for x in paginate(data.get("standings", []), "Таблица"):
                d["r"].append(
                    [
                        short(x.get("name")),
                        "Место " + str(x.get("place", "—")),
                        str(x.get("stars", "—")) + " ★",
                    ]
                )
            button("Раунды 1–7", "cwl", round=1)
    elif kind == "rating":
        mode = route.get("mode", "war")
        data = await fetch("/next-war", {"mode": mode, "size": 15})
        d["t"] = "Рейтинг " + ("КВ" if mode == "war" else "ЛВК")
        for x in paginate(data.get("clanRanking", []), "Игроки"):
            d["r"].append(
                [
                    short(x.get("name")),
                    short(x.get("tag")),
                    short(x.get("selectionScore")),
                ]
            )
        d["f"] = "Баллы backend; опыт не равен качеству атак."
        button("КВ", "rating", mode="war")
        button("ЛВК", "rating", mode="cwl")
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
    if kind != "home":
        button("⌂ Штаб", "home")
    return d, buttons
