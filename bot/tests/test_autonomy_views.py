import asyncio
from types import SimpleNamespace
from app.widgets import Widgets
from app.profile_ui import highest_role


def test_current_clan_role_excludes_departed_leader():
    assert (
        highest_role(
            [
                {"role": "leader", "inConfiguredClan": False},
                {"role": "admin", "inConfiguredClan": True},
            ]
        )
        == "admin"
    )


def test_new_widget_views_obey_state_budget(tmp_path):
    async def run():
        w = Widgets(tmp_path / "db", SimpleNamespace(get_user_accounts=lambda *a: []))
        members = [
            {
                "tag": f"#P{i}",
                "name": "ОченьДлинноеИмяИгрока" + str(i),
                "townHallLevel": 18,
                "accountStrength": 188.99,
                "coverage": 100,
                "thContribution": 180,
                "contributions": {
                    "heroes": 2.7,
                    "troops": 2.7,
                    "spells": 1.35,
                    "siege": 0.9,
                    "equipment": 1.35,
                },
            }
            for i in range(45)
        ]

        async def fetch(path, params=None):
            if path == "/account-strength":
                return {"players": members}
            if path == "/clan":
                return {"memberList": members}
            if path.startswith("/player/"):
                return members[0]
            if path.startswith("/raid-history/"):
                return {
                    "participations": 20,
                    "attacks": 120,
                    "gold": 999999,
                    "goldPerAttack": 999.9,
                }
            return {"attacksReceived": 123, "defenseIndex": 66.7}

        w.fetch = fetch
        for screen in (
            "strength",
            "strength_detail",
            "profile",
            "raid_history",
            "defense",
        ):
            raw, actions = await w.data(
                {"s": screen, "tag": "#P0", "p": 1}, 7, "a" * 12, 42
            )
            assert len(raw.encode()) <= 1024
            assert actions
        await w.close()

    asyncio.run(run())
