"""Additive private-screen storage; no existing tables or account data changed."""

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager


class StaleWidget(ValueError):
    pass


class WidgetStore:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS widget_screens(
                token TEXT PRIMARY KEY, owner INTEGER NOT NULL, message INTEGER,
                revision INTEGER NOT NULL, actions TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS widget_preferences(owner INTEGER PRIMARY KEY, mode TEXT NOT NULL);""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def mode(self, owner):
        with self.connect() as db:
            row = db.execute(
                "SELECT mode FROM widget_preferences WHERE owner=?", (owner,)
            ).fetchone()
            return row[0] if row else "widgets"

    def preference(self, owner, mode):
        if mode not in {"classic", "widgets"}:
            raise ValueError("Invalid mode")
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO widget_preferences VALUES (?,?)", (owner, mode)
            )
            db.execute("DELETE FROM widget_screens WHERE owner=?", (owner,))

    def create(self, owner):
        if type(owner) is not int or owner <= 0:
            raise ValueError("Private owner required")
        token = secrets.token_hex(6)
        with self.connect() as db:
            db.execute(
                "DELETE FROM widget_screens WHERE expires<? OR owner=?",
                (time.time(), owner),
            )
            db.execute(
                "INSERT INTO widget_screens VALUES (?,?,NULL,0,?,?)",
                (token, owner, "{}", time.time() + 86400),
            )
        return token

    def publish(self, token, revision, actions, message=None):
        with self.connect() as db:
            result = db.execute(
                "UPDATE widget_screens SET actions=?,message=COALESCE(?,message) WHERE token=? AND revision=?",
                (json.dumps(actions), message, token, revision),
            )
            if result.rowcount != 1:
                raise StaleWidget("Открой новый штаб: /menu.")

    def claim(self, query):
        try:
            token, revision, action = query["data"].split(":")
            owner = query["from"]["id"]
            if (
                query["chat"]["type"] != "private"
                or query["chat"]["id"] != owner
                or query.get("inline_message_id")
            ):
                raise ValueError()
            if (query.get("type") or {}).get("type") != "data" or len(
                query["data"].encode()
            ) > 64:
                raise ValueError()
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT owner,message,revision,actions,expires FROM widget_screens WHERE token=?",
                    (token,),
                ).fetchone()
                if (
                    not row
                    or row[:3] != (owner, query["message_id"], int(revision))
                    or row[4] < time.time()
                ):
                    raise ValueError()
                route = json.loads(row[3])[action]
                db.execute(
                    "UPDATE widget_screens SET revision=revision+1,actions=? WHERE token=?",
                    ("{}", token),
                )
            return token, int(revision) + 1, owner, route
        except (ValueError, KeyError, TypeError):
            raise StaleWidget("Экран устарел или недоступен. Открой /menu.") from None
