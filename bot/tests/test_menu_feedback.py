import asyncio
from types import SimpleNamespace
from telegram.error import BadRequest
from app.widgets import Widgets, record
from app.navigation import navigator
from ui_harness import Harness, GROUP


def test_group_command_new_reply_but_buttons_edit(monkeypatch,tmp_path):
    async def run():
        h=Harness(monkeypatch,tmp_path)
        await h.initialize()
        try:
            h.chat=GROUP
            await h.text('/menu')
            first=navigator.screens[(h.chat,h.user)].message.message_id
            await h.text('/menu')
            second=navigator.screens[(h.chat,h.user)].message.message_id
            assert second != first
            await h.click('menu_ratings')
            assert navigator.screens[(h.chat,h.user)].message.message_id==second
        finally:
            await h.shutdown()
    asyncio.run(run())


def test_help_pages_fit_and_have_return_action(tmp_path):
    async def run():
        w=Widgets(tmp_path / 'db',SimpleNamespace(get_user_accounts=lambda *a:[]))
        for page in range(4):
            data,actions=await w.data({'s':'strength_help','page':page,'back':{'s':'strength_detail','tag':'#P0'}},7,'a'*12,42)
            assert len(data.encode())<=1024
            assert {'s':'strength_detail','tag':'#P0'} in actions.values()
        await w.close()
    asyncio.run(run())


def test_rejection_hints_do_not_log_raw_details(caplog):
    record('sendWidget',BadRequest('WIDGET_DATA_INVALID secret-token-personal-name-12345'))
    assert 'data,invalid,widget' in caplog.text
    assert 'secret-token' not in caplog.text and '12345' not in caplog.text
