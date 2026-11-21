"""Conservative roster decisions. No Telegram side effects; callers must recheck rights."""

import sqlite3
from contextlib import contextmanager

ROLES = {"member": 0, "admin": 1, "coLeader": 2, "leader": 3}


def highest_role(accounts, members):
    roles = [m.get("role", "member") for m in members if m.get("tag") in accounts]
    return max(roles, key=lambda role: ROLES.get(role, -1), default=None)


class AccessPolicy:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS access_guests (
                    group_id INTEGER, user_id INTEGER, granted_by INTEGER NOT NULL,
                    expires REAL, PRIMARY KEY(group_id,user_id));
                CREATE TABLE IF NOT EXISTS access_observations (
                    group_id INTEGER, user_id INTEGER, first_absent REAL NOT NULL,
                    last_check REAL NOT NULL, PRIMARY KEY(group_id,user_id));
                CREATE TABLE IF NOT EXISTS access_events (
                    group_id INTEGER, user_id INTEGER, actor INTEGER, kind TEXT, at REAL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def guest(self, group, user, actor, expires, now):
        if expires is not None and expires <= now:
            raise ValueError("Expiration must be in the future")
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO access_guests VALUES (?,?,?,?)",
                (group, user, actor, expires),
            )
            db.execute(
                "DELETE FROM access_observations WHERE group_id=? AND user_id=?",
                (group, user),
            )
            db.execute(
                "INSERT INTO access_events VALUES (?,?,?,?,?)",
                (group, user, actor, "guest_granted", now),
            )

    def revoke(self, group, user, actor, now):
        with self.connect() as db:
            db.execute(
                "DELETE FROM access_guests WHERE group_id=? AND user_id=?",
                (group, user),
            )
            db.execute(
                "DELETE FROM access_observations WHERE group_id=? AND user_id=?",
                (group, user),
            )
            db.execute(
                "INSERT INTO access_events VALUES (?,?,?,?,?)",
                (group, user, actor, "guest_revoked", now),
            )

    def observe(self, group, user, accounts, roster, clan_tag, now, delay=86400):
        """Only distinct fresh, complete successful snapshots count as evidence.

        Caller must obtain an uncached roster; None denotes any fetch failure.
        Unlinked people never become removal candidates automatically.
        """
        if delay < 300:
            raise ValueError("Confirmation delay must be at least five minutes")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            guest = db.execute(
                "SELECT expires FROM access_guests WHERE group_id=? AND user_id=?",
                (group, user),
            ).fetchone()
            if guest and (guest[0] is None or guest[0] > now):
                return "guest"
            if not accounts:
                return "registration"
            members = roster.get("memberList") if isinstance(roster, dict) else None
            valid = (
                isinstance(members, list)
                and bool(members)
                and roster.get("tag") == clan_tag
                and roster.get("members") == len(members)
                and all(
                    isinstance(m, dict) and isinstance(m.get("tag"), str) and m["tag"]
                    for m in members
                )
            )
            if not valid or len({m["tag"] for m in members}) != len(members):
                # Break the confirmation chain on failed/partial observations.
                db.execute(
                    "DELETE FROM access_observations WHERE group_id=? AND user_id=?",
                    (group, user),
                )
                return "unknown"
            if set(accounts) & {m["tag"] for m in members}:
                db.execute(
                    "DELETE FROM access_observations WHERE group_id=? AND user_id=?",
                    (group, user),
                )
                return "member"
            previous = db.execute(
                "SELECT first_absent,last_check FROM access_observations WHERE group_id=? AND user_id=?",
                (group, user),
            ).fetchone()
            if previous is None:
                db.execute(
                    "INSERT INTO access_observations VALUES (?,?,?,?)",
                    (group, user, now, now),
                )
                return "pending"
            if now <= previous[1]:
                return "pending"
            db.execute(
                "UPDATE access_observations SET last_check=? WHERE group_id=? AND user_id=?",
                (now, group, user),
            )
            return "candidate" if now - previous[0] >= delay else "pending"
