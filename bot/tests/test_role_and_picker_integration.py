"""Integration-level role inheritance and live nickname picker checks.

These use the real SQLite storage and mock only CoC HTTP and Telegram transport.
"""
import asyncio
from types import SimpleNamespace

from app import bot as bot_module
from app.bindings_storage import Binding, BindingsStorage


class StubHTTPClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def binding(tag: str, owner: int = 777) -> Binding:
    return Binding(
        telegram_user_id=owner, group_id=-1001, coc_player_tag=tag,
        telegram_username="member", telegram_full_name="Player",
        created_at="2026-11-05T12:00:00Z",
    )


def setup(monkeypatch, tmp_path, roster):
    storage = BindingsStorage(str(tmp_path / "links.sqlite3"))
    monkeypatch.setattr(
        bot_module, "settings",
        SimpleNamespace(clan_group_id=-1001, request_timeout_seconds=15),
    )
    monkeypatch.setattr(bot_module.httpx, "AsyncClient", StubHTTPClient)

    async def fake_fetch(client, url):
        assert url == "/clan"
        return {"memberList": roster}

    monkeypatch.setattr(bot_module, "fetch_json", fake_fetch)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(type="private"),
        effective_user=SimpleNamespace(
            id=777, full_name="Alex", username="alex", first_name="Alex"
        ),
        message=None,
        callback_query=None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"storage": storage}),
        user_data={}, args=[],
    )
    return storage, update, context


def test_privileges_use_highest_verified_game_role_not_telegram_admin(monkeypatch, tmp_path):
    roster = [
        {"tag": "#AAA", "name": "Alt A", "role": "admin"},
        {"tag": "#BBB", "name": "Alt B", "role": "admin"},
        {"tag": "#CCC", "name": "Alt C", "role": "coLeader"},
        {"tag": "#DDD", "name": "Alt D", "role": "leader"},
    ]
    storage, update, context = setup(monkeypatch, tmp_path, roster)
    storage.upsert_binding(binding("#AAA"))
    storage.upsert_binding(binding("#BBB"))
    assert not asyncio.run(bot_module.has_captain_access(update, context))
    # No telegram admin flag is present or relevant.
    storage.upsert_binding(binding("#CCC"))
    assert asyncio.run(bot_module.has_captain_access(update, context))
    storage.unlink_account(-1001, "#CCC", owner_id=777)
    assert not asyncio.run(bot_module.has_captain_access(update, context))
    storage.upsert_binding(binding("#DDD"))
    assert asyncio.run(bot_module.has_captain_access(update, context))
    # Removing the last leadership alt immediately revokes privileges.
    storage.unlink_account(-1001, "#DDD", owner_id=777)
    assert not asyncio.run(bot_module.has_captain_access(update, context))


def test_picker_excludes_verified_tags_for_other_and_own_alts(monkeypatch, tmp_path):
    roster = [
        {"tag": "#AAA", "name": "Already mine", "townHallLevel": 18},
        {"tag": "#BBB", "name": "Another owner", "townHallLevel": 17},
        {"tag": "#CCC", "name": "Available", "townHallLevel": 16},
    ]
    storage, update, context = setup(monkeypatch, tmp_path, roster)
    storage.upsert_binding(binding("#AAA"))
    storage.upsert_binding(binding("#BBB", owner=999))
    shown = []

    async def fake_send(update, message, **kwargs):
        shown.append((message, kwargs))
        return None

    monkeypatch.setattr(bot_module, "send_or_edit_message", fake_send)
    asyncio.run(bot_module.show_player_picker(update, context))
    assert [m["tag"] for m in context.user_data["picker_members"]] == ["#CCC"]
    keyboard = shown[0][1]["reply_markup"]
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any("Available" in b.text for b in buttons)
    assert all("Already mine" not in b.text and "Another owner" not in b.text for b in buttons)


def test_stale_picker_callback_cannot_select_newly_taken_tag(monkeypatch, tmp_path):
    storage, update, context = setup(monkeypatch, tmp_path, [])
    context.user_data["picker_members"] = [{"tag": "#AAA", "name": "Old choice"}]
    storage.upsert_binding(binding("#AAA", owner=999))
    texts = []
    refreshed = []

    async def fake_answer():
        return None

    async def fake_edit(message, **kwargs):
        texts.append(message)

    async def fake_refresh(update, context):
        refreshed.append(True)

    async def fail_bind(*args):
        raise AssertionError("Taken tag must not enter verification")

    update.callback_query = SimpleNamespace(
        data="pick:player:0", answer=fake_answer, edit_message_text=fake_edit,
    )
    async def fake_send(update, message, **kwargs):
        texts.append(message)
    monkeypatch.setattr(bot_module, "send_or_edit_message", fake_send)
    monkeypatch.setattr(bot_module, "show_player_picker", fake_refresh)
    monkeypatch.setattr(bot_module, "process_binding", fail_bind)
    asyncio.run(bot_module.roster_callback(update, context))
    assert refreshed == [True]
    assert any("уже привязан" in text for text in texts)
