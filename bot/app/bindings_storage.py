from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Binding:
    telegram_user_id: int
    group_id: int
    coc_player_tag: str
    telegram_username: str | None
    telegram_full_name: str
    created_at: str


class BindingsStorage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    telegram_user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    coc_player_tag TEXT NOT NULL,
                    telegram_username TEXT NULL,
                    telegram_full_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_id, telegram_user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bindings_group_tag
                ON bindings(group_id, coc_player_tag)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_cooldowns (
                    group_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    last_reminded_at TEXT NOT NULL,
                    PRIMARY KEY(group_id, telegram_user_id)
                )
                """
            )
            conn.commit()

    def upsert_binding(self, binding: Binding) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bindings (
                    telegram_user_id,
                    group_id,
                    coc_player_tag,
                    telegram_username,
                    telegram_full_name,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, telegram_user_id) DO UPDATE SET
                    coc_player_tag=excluded.coc_player_tag,
                    telegram_username=excluded.telegram_username,
                    telegram_full_name=excluded.telegram_full_name,
                    created_at=excluded.created_at
                """,
                (
                    binding.telegram_user_id,
                    binding.group_id,
                    binding.coc_player_tag,
                    binding.telegram_username,
                    binding.telegram_full_name,
                    binding.created_at,
                ),
            )
            conn.commit()

    def delete_binding(self, group_id: int, telegram_user_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM bindings WHERE group_id = ? AND telegram_user_id = ?",
                (group_id, telegram_user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_binding(self, group_id: int, telegram_user_id: int) -> Binding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bindings WHERE group_id = ? AND telegram_user_id = ?",
                (group_id, telegram_user_id),
            ).fetchone()
        return self._row_to_binding(row)

    def get_bindings_for_group(self, group_id: int) -> list[Binding]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bindings WHERE group_id = ?",
                (group_id,),
            ).fetchall()
        return [binding for row in rows if (binding := self._row_to_binding(row))]

    def get_bindings_for_tags(self, group_id: int, tags: Iterable[str]) -> list[Binding]:
        tags_list = list(tags)
        if not tags_list:
            return []
        placeholders = ",".join(["?"] * len(tags_list))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM bindings WHERE group_id = ? AND coc_player_tag IN ({placeholders})",
                (group_id, *tags_list),
            ).fetchall()
        return [binding for row in rows if (binding := self._row_to_binding(row))]

    def get_group_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT group_id FROM bindings").fetchall()
        return [row[0] for row in rows]

    def get_cooldowns(self, group_id: int, user_ids: Iterable[int]) -> dict[int, datetime]:
        user_list = list(user_ids)
        if not user_list:
            return {}
        placeholders = ",".join(["?"] * len(user_list))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT telegram_user_id, last_reminded_at FROM reminder_cooldowns "
                f"WHERE group_id = ? AND telegram_user_id IN ({placeholders})",
                (group_id, *user_list),
            ).fetchall()
        results: dict[int, datetime] = {}
        for row in rows:
            try:
                results[int(row[0])] = datetime.fromisoformat(row[1])
            except (TypeError, ValueError):
                continue
        return results

    def set_cooldowns(self, group_id: int, user_ids: Iterable[int], timestamp: datetime) -> None:
        payload = [(group_id, user_id, timestamp.isoformat()) for user_id in user_ids]
        if not payload:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO reminder_cooldowns (group_id, telegram_user_id, last_reminded_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id, telegram_user_id) DO UPDATE SET
                    last_reminded_at=excluded.last_reminded_at
                """,
                payload,
            )
            conn.commit()

    @staticmethod
    def _row_to_binding(row: sqlite3.Row | None) -> Binding | None:
        if row is None:
            return None
        return Binding(
            telegram_user_id=int(row["telegram_user_id"]),
            group_id=int(row["group_id"]),
            coc_player_tag=str(row["coc_player_tag"]),
            telegram_username=row["telegram_username"],
            telegram_full_name=str(row["telegram_full_name"]),
            created_at=str(row["created_at"]),
        )
