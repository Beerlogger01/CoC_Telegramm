import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.clan_access import ClanAccess
from app.settings import settings


def test_dry_run_multialt_and_confirmed_removal(tmp_path,monkeypatch):
    async def run():
        monkeypatch.setattr(settings,"clan_group_id",1)
        monkeypatch.setattr(settings,"coc_clan_tag","#CLAN")
        bindings=SimpleNamespace(get_bindings_for_group=lambda g:[SimpleNamespace(telegram_user_id=2)],get_user_accounts=lambda g,u:[SimpleNamespace(coc_player_tag="#MAIN")])
        access=ClanAccess(tmp_path / "db",bindings)
        roster={"tag":"#CLAN","members":1,"memberList":[{"tag":"#OTHER"}]}
        bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),send_message=AsyncMock(),ban_chat_member=AsyncMock(),unban_chat_member=AsyncMock())
        context=SimpleNamespace(bot=bot)
        monkeypatch.setattr("app.clan_access.time.time",lambda:100.)
        assert (await access.scan(context,roster,False))[0][1]=="pending"
        monkeypatch.setattr("app.clan_access.time.time",lambda:100000.)
        assert (await access.scan(context,roster,False))[0][1]=="candidate"
        bot.ban_chat_member.assert_not_called()
        monkeypatch.setattr("app.clan_access.time.time",lambda:100001.)
        await access.scan(context,roster,True)
        bot.ban_chat_member.assert_awaited_once_with(1,2)
        bot.unban_chat_member.assert_awaited_once_with(1,2,only_if_banned=True)
        assert bindings.get_user_accounts(1,2)[0].coc_player_tag=="#MAIN"
        await access.client.aclose()
    asyncio.run(run())


def test_api_failure_never_removes_and_clears_evidence(tmp_path,monkeypatch):
    async def run():
        monkeypatch.setattr(settings,"clan_group_id",1)
        access=ClanAccess(tmp_path / "db",SimpleNamespace())
        access.roster=AsyncMock(side_effect=TimeoutError())
        bot=SimpleNamespace(ban_chat_member=AsyncMock())
        with access.policy.connect() as db:
            db.execute("INSERT INTO access_observations VALUES (1,2,0,1)")
        await access.tick(SimpleNamespace(bot=bot))
        bot.ban_chat_member.assert_not_called()
        with access.policy.connect() as db:
            assert not db.execute("SELECT * FROM access_observations").fetchall()
        await access.client.aclose()
    asyncio.run(run())


def test_join_link_is_bound_to_target_and_current_role(tmp_path,monkeypatch):
    async def run():
        monkeypatch.setattr(settings,"clan_group_id",1)
        bindings=SimpleNamespace(get_user_accounts=lambda g,u:[SimpleNamespace(coc_player_tag="#LEADER")])
        access=ClanAccess(tmp_path / "db",bindings)
        access.roster=AsyncMock(return_value={"memberList":[{"tag":"#LEADER","role":"leader"}]})
        with access.policy.connect() as db:
            db.execute("INSERT INTO access_invites VALUES ('link',1,2,3,99999999999,99999999999,0)")
        bot=SimpleNamespace(approve_chat_join_request=AsyncMock(),revoke_chat_invite_link=AsyncMock())
        req=SimpleNamespace(chat=SimpleNamespace(id=1),from_user=SimpleNamespace(id=4),invite_link=SimpleNamespace(invite_link="link"))
        update=SimpleNamespace(chat_join_request=req)
        await access.join(update,SimpleNamespace(bot=bot))
        bot.approve_chat_join_request.assert_not_called()
        req.from_user.id=2
        await access.join(update,SimpleNamespace(bot=bot))
        await access.join(update,SimpleNamespace(bot=bot))
        bot.approve_chat_join_request.assert_awaited_once_with(1,2)
        await access.client.aclose()
    asyncio.run(run())
