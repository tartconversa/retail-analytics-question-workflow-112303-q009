"""SQLite 连接、事务、编号和审计日志的薄封装。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(
        self,
        target: str | Path | sqlite3.Connection,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        if isinstance(target, sqlite3.Connection):
            self.conn = target
        else:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path))
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.isolation_level = None
        self._clock = clock

    def now(self) -> str:
        return self._clock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.conn.in_transaction:
            # 嵌套调用复用外层事务；由最外层统一提交或回滚。
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @staticmethod
    def one(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> dict | None:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def all(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict]:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    @staticmethod
    def next_id(conn: sqlite3.Connection, name: str, prefix: str) -> str:
        row = conn.execute("SELECT value FROM id_sequences WHERE name = ?", (name,)).fetchone()
        if row is None:
            value = 1
            conn.execute("INSERT INTO id_sequences (name, value) VALUES (?, ?)", (name, value))
        else:
            value = int(row["value"]) + 1
            conn.execute("UPDATE id_sequences SET value = ? WHERE name = ?", (value, name))
        return f"{prefix}-{value:05d}"

    def audit(
        self,
        conn: sqlite3.Connection,
        *,
        actor_id: str,
        action: str,
        object_type: str,
        object_id: str,
        request_id: str = "",
        detail: dict | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events (occurred_at, actor_id, action, object_type, object_id, request_id, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.now(),
                actor_id,
                action,
                object_type,
                object_id,
                request_id,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
