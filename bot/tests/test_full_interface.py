import asyncio
import copy

from telegram.error import NetworkError

from app import bot as handlers
from app.navigation import navigator
from ui_harness import Harness, OWNER, GROUP


def test_complete_menu_profiles_ratings_history_and_cwl(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            await h.text("/menu")
            assert len(h.actions) == 8
            for section in [
                "menu_wars",
                "menu_ratings",
                "menu_community",
                "menu_help",
                "menu_staff",
            ]:
                await h.click(section)
                assert "menu_home" in h.actions
                await h.click("menu_home")
            await h.click("menu_wars")
            await h.click("menu_war")
            assert "Осталось" not in h.screen["text"]  # Ended war.
            await h.click("menu_wars")
            await h.click("menu_cwl")
            for n in range(1, 8):
                await h.click(f"cwl:round:{n}")
                assert f"раунд {n} / 7" in h.screen["text"]
                if n == 2:
                    assert "ещё не опубликован" in h.screen["text"]
                if n == 3:
                    assert "Third&lt;&amp;&gt;" in h.screen["text"]
            await h.click("cwl:table:0")
            await h.click("menu_cwl")
            await h.click("menu_wars")
            await h.click("menu_warlog")
            await h.click("warlog:list:1")
            await h.click("warlog:view:8:0")
            for n in range(1, 7):
                await h.click(f"warlog:view:8:{n}")
            assert "TH" in h.screen["text"]
            await h.click("warlog:list:1")
            await h.click("menu_wars")
            await h.click("menu_home")
            await h.click("menu_ratings")
            await h.click("menu_ratingcwl")
            assert "Рейтинг всего клана" in h.screen["text"]
            for n in range(1, 4):
                await h.click(f"lineup:ranking:{n}")
            await h.click("lineup:player:49")
            assert "49 Lex&lt;&amp;&gt;_*[]😀" in h.screen["text"]
            assert "Из чего сложился балл" in h.screen["text"]
            await h.click("lineup:ranking:3")
            await h.click("lineup:mode:war")
            assert "Рейтинг всего клана" in h.screen["text"]
            await h.click("lineup:size:0")
            await h.click("lineup:teamsize:45")
            assert "45/45" in h.screen["text"]
            await h.click("lineup:filters:0")
            for category in [
                "inactive",
                "optout",
                "needs_review",
                "twinks",
                "kick",
                "promotion",
                "bonus",
            ]:
                await h.click(f"lineup:{category}:0")
                await h.click("lineup:filters:0")
            await h.click("lineup:weights:0")
            await h.click("lineup:about:0")
            assert "15% уровень опыта" in h.screen["text"]
            await h.click("menu_home")
            await h.click("menu_topplayers")
            for n in range(1, 7):
                await h.click(f"players:page:{n}")
            await h.click("players:view:49")
            assert "49 Lex&lt;&amp;&gt;" in h.screen["text"]
            await h.click("players:equipment:49:0")
            await h.click("players:equipment:49:1")
            await h.click("players:equipment:49:2")
            assert "Equipment &amp; 28" in h.screen["text"]
            await h.click("players:view:49")
            await h.click("players:heroes:49:0")
            await h.click("players:heroes:49:1")
            assert "Hero&lt;13&gt;" in h.screen["text"]
            await h.click("players:page:6")
            await h.click("menu_home")
            await h.click("menu_profile")
            for n in range(1, 5):
                await h.click(f"acct:list:{n}")
            await h.click("acct:view:23")
            await h.click("acct:equipment:23:0")
            await h.click("acct:equipment:23:1")
            await h.click("acct:view:23")
            await h.click("acct:progress:23")
            await h.click("growth:days:30")
            await h.click("acct:view:23")
            chosen = h.app.user_data[OWNER]["own_profiles"][23]["tag"]
            await h.click("acct:primary:23")
            assert h.storage.get_binding(GROUP, OWNER).coc_player_tag == chosen
            await h.click("acct:view:23")
            await h.click("acct:delete:23")
            await h.click("acct:view:23")
            await h.click("acct:list:4")
            await h.click("menu_home")
            await h.click("menu_ratings")
            await h.click("menu_donate")
            for n in range(1, 4):
                await h.click(f"players:donate:{n}")
            await h.click("menu_ratings")
            await h.click("menu_hall")
            await h.click("menu_ratings")
            await h.click("menu_home")
            await h.click("menu_community")
            await h.click("menu_raids")
            await h.click("menu_community")
            await h.click("menu_polls")
            assert "Голосований пока нет" in h.screen["text"]
            assert (
                len(h.sends(OWNER)) == 1
            )  # Every command/loading step uses the same screen.
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_stale_foreign_duplicate_and_missing_message_callbacks(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            await h.text("/menu")
            old = copy.deepcopy(h.screen)
            raw = h.actions["menu_profile"]
            await h.click("menu_topplayers")
            latest = copy.deepcopy(h.screen)
            await h.click("", raw=raw, message=old)
            assert h.screen == latest and "устарела" in h.request.answers[-1]["text"]
            raw = h.actions["players:view:0"]
            msg = copy.deepcopy(h.screen)
            h.user = 202
            await h.click("", raw=raw, message=msg)
            assert "другим игроком" in h.request.answers[-1]["text"]
            h.user = OWNER
            update = await h.click("players:view:0")
            total = len(h.request.calls)
            await h.app.process_update(update)
            assert len(h.request.calls) == total
            key = (h.chat, h.screen["message_id"])
            del h.request.messages[key]
            await h.text("/menu")
            assert (
                len(h.sends(OWNER)) == 2
            )  # One replacement for a genuinely deleted screen.
            navigator.screens[OWNER].expires = 0
            await h.click("menu_profile")
            assert "устарела" in h.request.answers[-1]["text"]
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_readiness_draft_publish_and_duplicate_protection(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            await h.text("/lineup")
            await h.click("ops:new:war")
            await h.click("ops:newsize:20")
            await h.text("КВ в пятницу <&>")
            assert "20/20" in h.screen["text"]
            await h.click("ops:ready:1:0")
            await h.click("ops:answer:1:0:heroes")
            assert "Герои улучшаются" in h.screen["text"]
            await h.click("ops:view:1:0")
            await h.click("ops:publish:1")
            assert "не готов" in h.screen["text"] and not h.sends(GROUP)
            await h.click("ops:ready:1:0")
            await h.click("ops:answer:1:0:ready")
            await h.click("ops:view:1:0")
            await h.click("ops:edit:1:0")
            await h.click("ops:toggle:1:1")
            await h.click("ops:edit:1:1")
            await h.click("ops:edit:1:2")
            await h.click("ops:toggle:1:20")
            await h.click("ops:view:1:0")
            raw = h.actions["ops:publish:1"]
            msg = copy.deepcopy(h.screen)
            await h.click("ops:publish:1")
            assert len(h.sends(GROUP)) == 1
            await h.click("", raw=raw, message=msg)
            assert len(h.sends(GROUP)) == 1
            await h.click("ops:unlock:1")
            await h.click("ops:publish:1")
            assert len(h.sends(GROUP)) == 1  # Republish edits the published roster.
            await h.click("ops:closeask:1")
            await h.click("ops:close:1")
            assert "открытых сборов нет" in h.screen["text"]
            await h.click("ops:new:cwl")
            await h.text("ЛВК · раунд 7")
            assert "15/15" in h.screen["text"] and not h.ops.store.get(2)["answers"]
            h.request.failures["sendMessage"] = NetworkError("ambiguous timeout")
            await h.click("ops:publish:2")
            assert "повторная отправка заблокирована" in h.screen["text"]
            assert h.ops.store.get(2)["delivery"] == "sending"
            assert "ops:publish:2" not in h.actions
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_poll_wizard_results_listing_and_one_vote_per_owner(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            await h.text("/poll")
            await h.text("Когда КВ <&>?")
            await h.text("Пятница")
            await h.text("Суббота")
            await h.click("poll:remove")
            await h.text("Воскресенье")
            await h.click("poll:done")
            await h.click("poll:duration:60")
            await h.click("poll:publish")
            assert len(h.sends(GROUP)) == 1
            poll = h.polls.store.get(1)
            message = h.request.messages[GROUP, poll["message_id"]]
            await h.click("", raw="vote:1:0", message=message)
            await h.click("", raw="vote:1:1", message=message)
            assert h.polls.store.get(1)["counts"] == [0, 1]
            with h.polls.store.connect() as db:
                db.execute("UPDATE clan_polls SET closes_at=0 WHERE id=1")
            await h.polls.finish_due(h.context)
            await h.polls.finish_due(h.context)
            assert (
                len(h.sends(GROUP)) == 2
            )  # Publication + one final result, no repeat.
            await h.text("/menu")
            await h.click("menu_community")
            await h.click("menu_polls")
            await h.click("poll:view:1:0")
            assert (
                "Итоги голосования" in h.screen["text"]
                and "Воскресенье — 1" in h.screen["text"]
            )
            await h.click("poll:list:0")
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_empty_failed_sources_and_command_cancellation(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            h.empty = True
            for command in [
                "/menu",
                "/topplayers",
                "/donate",
                "/hall",
                "/cwl",
                "/war",
                "/history",
                "/rating",
                "/raids",
            ]:
                await h.text(command)
                assert h.actions
            h.empty = False
            for path, command in [
                ("/clan", "/clan"),
                ("/next-war", "/rating"),
                ("/war", "/war"),
                ("/cwl", "/cwl"),
                ("/war-results", "/history"),
            ]:
                h.fail_paths = {path}
                await h.text(command)
                assert "menu_home" in h.actions
            h.fail_paths = set()
            await h.text("/poll")
            await h.text("Question")
            await h.text("/menu")
            await h.text("Обычный текст")
            assert not h.app.user_data[OWNER].get("poll_draft")
            await h.text("/bind")
            await h.click("bind_cancel")
            assert not h.app.user_data[OWNER].get("awaiting_tag")
            assert len(h.sends(OWNER)) == 1
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_binding_picker_every_page_escapes_names_and_cancel_returns(
    monkeypatch, tmp_path
):
    async def scenario():
        h = Harness(monkeypatch, tmp_path, own=0)
        h.players[0]["clan"] = {"tag": "#PPP"}
        for p in h.players:
            p["clan"] = {"tag": "#PPP"}
        monkeypatch.setattr(handlers.settings, "require_player_verification", True)
        await h.initialize()
        try:
            await h.text("/start")
            await h.click("bind_start")
            for n in range(1, 7):
                await h.click(f"pick:page:{n}")
            await h.click("pick:player:49")
            assert "API Token" in h.screen["text"]
            assert h.app.user_data[OWNER]["awaiting_player_token"]
            await h.click("bind_cancel")
            await h.text("/bind")
            await h.click("pick:manual:0")
            await h.text("неправильный тег")
            assert not h.storage.get_user_accounts(GROUP, OWNER)
            await h.text("/menu")
            assert not h.app.user_data[OWNER].get("awaiting_player_token")
            assert len(h.sends(OWNER)) == 1
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_not_modified_markup_recovery_and_staff_children(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            await h.text("/menu")
            h.request.failures["editMessageText"] = (
                "Bad Request: message is not modified"
            )
            await h.click("menu_staff")
            for action in [
                "staff:promotion",
                "staff:kick",
                "staff:bonus",
                "menu_captain",
                "menu_weights",
            ]:
                await h.click(action)
                assert "menu_home" in h.actions
                await h.click("menu_home")
                await h.click("menu_staff")
            assert len(h.sends(OWNER)) == 1
            assert any(name == "editMessageReplyMarkup" for name, _ in h.request.calls)
            # An actual parse rejection is retried as plain text with the same valid keyboard.
            h.request.failures["editMessageText"] = "Bad Request: can't parse entities"
            await h.click("menu_home")
            await h.click("menu_ratings")
            assert "menu_ratingcwl" in h.actions
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_publish_revalidates_role_status_and_readiness_revision(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            await h.text("/lineup")
            await h.click("ops:new:cwl")
            await h.text("Раунд 4")
            h.ops.store.answer(1, h.players[0]["tag"], OWNER, "ready")
            await h.click("ops:publish:1")
            assert "изменились" in h.screen["text"] and not h.sends(GROUP)
            h.players[1]["eligibility"] = "optout"
            await h.click("ops:publish:1")
            assert "недопущенного" in h.screen["text"] and not h.sends(GROUP)
            h.players[1]["eligibility"] = "candidate"
            h.players[0]["role"] = "member"
            await h.click("ops:publish:1")
            assert "руководство" in h.screen["text"] and not h.sends(GROUP)
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_token_retry_binding_success_and_unlink_confirmation_share_one_screen(
    monkeypatch, tmp_path
):
    async def scenario():
        h = Harness(monkeypatch, tmp_path, own=0)
        for player in h.players:
            player["clan"] = {"tag": "#PPP"}
        monkeypatch.setattr(handlers.settings, "require_player_verification", True)
        monkeypatch.setattr(handlers.settings, "enforce_clan_membership", True)
        await h.initialize()
        try:
            await h.text("/bind")
            await h.click("pick:player:0")
            tag = h.app.user_data[OWNER]["pending_coc_tag"]
            h.token_valid = False
            await h.text("invalid-offline-token")
            assert "Токен не подходит" in h.screen["text"]
            assert not h.storage.get_user_accounts(GROUP, OWNER)
            h.token_valid = True
            await h.text("valid-offline-token")
            assert h.storage.get_binding(GROUP, OWNER).coc_player_tag == tag
            assert "привязан" in h.screen["text"]
            assert len(h.sends(OWNER)) == 1
            assert all("offline-token" not in p["text"] for p in h.sends())
            await h.text("/profile")
            await h.click("acct:view:0")
            await h.click("acct:delete:0")
            assert h.storage.get_binding(GROUP, OWNER)
            await h.click("acct:confirm:0")
            assert not h.storage.get_user_accounts(GROUP, OWNER)
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_command_menu_and_remaining_attacks_are_reachable(monkeypatch, tmp_path):
    async def scenario():
        h = Harness(monkeypatch, tmp_path)
        await h.initialize()
        try:
            for command in [
                "/start",
                "/help",
                "/clan",
                "/player",
                "/mytag",
                "/settings",
                "/captain",
                "/staff",
                "/games",
                "/ping",
                "/weights",
                "/primary",
                "/unlink",
                "/adminunlink",
                "/lastseen",
                "/setweight",
                "/unbind",
                "/chatid",
                "/grouplink",
            ]:
                await h.text(command)
                assert h.actions, command
            await h.text("/player")
            await h.click("inspect:equipment:0:0")
            await h.click("inspect:equipment:0:1")
            await h.click("inspect:equipment:0:2")
            assert "Equipment &amp; 28" in h.screen["text"]
            await h.click("inspect:view:0")
            await h.click("menu_topplayers")
            await h.text("/war")
            for n in range(1, 5):
                await h.click(f"warpage:{n}")
            assert "49 Lex&lt;&amp;&gt;" in h.screen["text"]
            await h.click("menu_wars")
            assert len(h.sends(OWNER)) == 1
        finally:
            await h.shutdown()

    asyncio.run(scenario())
