from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from app.operations import WarPlanStore, PlanConflict, answer_status, plan_text
from app.bindings_storage import Binding, BindingsStorage
from app.navigation import plain_text


def create(store, size=15):
    roster = [
        {
            "tag": f"#{i}",
            "name": "😀" * 40,
            "townHallLevel": 18,
            "eligibility": "candidate",
        }
        for i in range(50)
    ]
    return store.create(
        "#CLAN",
        "war",
        "Название <&>",
        size,
        roster,
        [p["tag"] for p in roster[:size]],
        1,
    )


def test_plans_readiness_survive_restart_but_not_new_war_or_new_owner(tmp_path):
    path = str(tmp_path / "db")
    store = WarPlanStore(path)
    bindings = BindingsStorage(path)
    bindings.upsert_binding(Binding(1, -100, "#0", None, "Lex", "2026"))
    plan_id = create(store)
    store.answer(plan_id, "#0", 1, "ready")
    restored = WarPlanStore(path)
    plan = restored.get(plan_id)
    assert answer_status(plan, "#0", bindings, -100) == "ready"
    bindings.unlink_account(-100, "#0", owner_id=1)
    bindings.upsert_binding(Binding(2, -100, "#0", None, "New owner", "2026"))
    assert answer_status(plan, "#0", bindings, -100) == "unknown"
    new = create(restored)
    assert not restored.get(new)["answers"] and not restored.get(plan_id)["open"]
    with pytest.raises(PlanConflict):
        restored.answer(plan_id, "#0", 1, "ready")


def test_atomic_publish_claim_and_stale_editor_cannot_overwrite(tmp_path):
    store = WarPlanStore(str(tmp_path / "db"))
    i = create(store)

    def claim(_):
        try:
            store.change(i, 1, 1, claim=True)
            return True
        except PlanConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, range(2))) == 1
    assert store.get(i)["delivery"] == "sending"
    with pytest.raises(PlanConflict):
        store.change(i, 2, 1, unlock=True)
    store.delivered(i, -100, 99)
    assert WarPlanStore(store.path).get(i)["message_id"] == 99
    store.change(i, 2, 1, unlock=True)
    with pytest.raises(PlanConflict):
        store.change(i, 2, 1, tag="#1")
    store.change(i, 3, 1, tag="#1")
    with pytest.raises(ValueError, match="14/15"):
        store.change(i, 4, 1, claim=True)


def test_expired_plans_reject_answers_and_full_roster_is_never_truncated(tmp_path):
    store = WarPlanStore(str(tmp_path / "db"))
    i = create(store, size=50)
    bindings = BindingsStorage(store.path)
    text = plan_text(store.get(i), bindings, -100, all_members=True)
    assert "50." in text and "Продолжение" not in text
    assert len(plain_text(text).encode("utf-16-le")) // 2 < 4096
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE war_plans SET expires=0")
    with pytest.raises(PlanConflict):
        store.answer(i, "#0", 1, "ready")
    with pytest.raises(PlanConflict):
        store.change(i, 1, 1, claim=True)
