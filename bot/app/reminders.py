"""Durable at-most-once reminder claims for each war, owner and urgency stage."""

import sqlite3
from pathlib import Path


class ReminderStore:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=10) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS war_reminders (
                chat_id INTEGER NOT NULL, war_id TEXT NOT NULL, stage TEXT NOT NULL,
                user_id INTEGER NOT NULL, PRIMARY KEY(chat_id,war_id,stage,user_id))""")

    def claim(self, chat_id, war_id, stage, user_ids):
        claimed = []
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            for user_id in dict.fromkeys(user_ids):
                if db.execute(
                    "INSERT OR IGNORE INTO war_reminders VALUES(?,?,?,?)",
                    (chat_id, war_id, stage, user_id),
                ).rowcount:
                    claimed.append(user_id)
        return claimed
