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

        with self.connect() as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(widget_screens)")
            }
            for name, definition in [
                ("pending", "TEXT"),
                ("lease", "REAL NOT NULL DEFAULT 0"),
            ]:
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE widget_screens ADD COLUMN {name} {definition}"
                    )

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
                "DELETE FROM widget_screens WHERE expires<?",
                (time.time(),),
            )
            db.execute(
                "INSERT INTO widget_screens(token,owner,message,revision,actions,expires) VALUES (?,?,NULL,0,?,?)",
                (token, owner, "{}", time.time() + 86400),
            )
            db.execute(
                "DELETE FROM widget_screens WHERE owner=? AND token NOT IN (SELECT token FROM widget_screens WHERE owner=? ORDER BY expires DESC,rowid DESC LIMIT 5)",
                (owner, owner),
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

    def begin(self, query):
        """Reserve a generation. A prepared delivery is replayed, never re-executed."""
        try:
            token, rev, action = query["data"].split(":")
            rev = int(rev)
            owner = query["from"]["id"]
            if (
                query["chat"]["type"] != "private"
                or query["chat"]["id"] != owner
                or query.get("inline_message_id")
            ):
                raise PermissionError("PERMISSION_DENIED")
            if (query.get("type") or {}).get("type") != "data" or len(
                query["data"].encode()
            ) > 64:
                raise ValueError()
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT owner,message,revision,actions,expires,pending,lease FROM widget_screens WHERE token=?",
                    (token,),
                ).fetchone()
                if not row or row[4] < time.time():
                    raise StaleWidget("WIDGET_EXPIRED")
                if row[:2] != (owner, query["message_id"]):
                    raise PermissionError("PERMISSION_DENIED")
                pending = json.loads(row[5]) if row[5] else None
                if pending and pending["base"] == rev and pending.get("data"):
                    return token, rev + 1, owner, pending["route"], pending
                if row[2] != rev:
                    raise StaleWidget("WIDGET_STALE")
                if row[6] > time.time():
                    raise StaleWidget("WIDGET_BUSY")
                route = json.loads(row[3])[action]
                pending = {"base": rev, "route": route}
                db.execute(
                    "UPDATE widget_screens SET pending=?,lease=? WHERE token=?",
                    (json.dumps(pending), time.time() + 15, token),
                )
            return token, rev + 1, owner, route, None
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, StaleWidget):
                raise
            raise StaleWidget("WIDGET_STALE") from None

    def prepare(self, token, revision, route, data, actions):
        pending = {
            "base": revision - 1,
            "route": route,
            "data": data,
            "actions": actions,
        }
        with self.connect() as db:
            result = db.execute(
                "UPDATE widget_screens SET pending=?,lease=0 WHERE token=? AND revision=?",
                (json.dumps(pending), token, revision - 1),
            )
            if result.rowcount != 1:
                raise StaleWidget("WIDGET_STALE")
        return pending

    def delivered(self, token, revision, actions):
        with self.connect() as db:
            result = db.execute(
                "UPDATE widget_screens SET revision=?,actions=?,lease=0 WHERE token=? AND revision=?",
                (revision, json.dumps(actions), token, revision - 1),
            )
        return result.rowcount == 1

    def release(self, token):
        with self.connect() as db:
            db.execute(
                "UPDATE widget_screens SET lease=0 WHERE token=? AND (pending IS NULL OR json_extract(pending,'$.data') IS NULL)",
                (token,),
            )
