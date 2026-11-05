import asyncio
from datetime import datetime, timedelta, timezone

from app import bot as handlers
from app.reminders import ReminderStore
from ui_harness import Harness, OWNER, GROUP


def test_reminders_include_second_attack_and_alts_once_per_war_stage(
    monkeypatch, tmp_path
):
    async def scenario():
        h = Harness(monkeypatch, tmp_path, own=2)
        await h.initialize()
        try:
            h.war["state"] = "inWar"
            h.war["endTime"] = (
                datetime.now(timezone.utc) + timedelta(hours=2)
            ).strftime("%Y%m%dT%H%M%S.000Z")
            await handlers.war_reminder_job(h.context)
            assert len(h.sends(GROUP)) == 1
            text = h.sends(GROUP)[0]["text"]
            assert "(2)" in text and "(1)" in text
            assert text.count(f"tg://user?id={OWNER}") == 1
            h.app.bot_data["reminders"] = ReminderStore(h.db)
            await handlers.war_reminder_job(h.context)
            assert len(h.sends(GROUP)) == 1
            h.war["endTime"] = (
                datetime.now(timezone.utc) + timedelta(minutes=20)
            ).strftime("%Y%m%dT%H%M%S.000Z")
            await handlers.war_reminder_job(h.context)
            assert len(h.sends(GROUP)) == 2
            h.war["startTime"] = "20261105T180000Z"
            await handlers.war_reminder_job(h.context)
            assert (
                len(h.sends(GROUP)) == 3
            )  # A new war is independent of prior cooldowns.
        finally:
            await h.shutdown()

    asyncio.run(scenario())


def test_claim_is_durable_even_if_telegram_timeout_was_ambiguous(tmp_path):
    store = ReminderStore(str(tmp_path / "db"))
    assert store.claim(-100, "war1", "window", [1, 1, 2]) == [1, 2]
    assert ReminderStore(store.path).claim(-100, "war1", "window", [1, 2]) == []
    assert store.claim(-100, "war1", "last30m", [1]) == [1]
