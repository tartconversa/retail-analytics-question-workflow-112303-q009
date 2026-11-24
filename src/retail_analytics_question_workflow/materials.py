"""把合成资料导入 SQLite。

资料目录结构：

    reference/staff.csv
    reference/stores.csv
    reference/datasets.csv
    reference/metric_catalog.csv
    reference/access_policies.csv
    reference/business_events.csv
    reference/data_versions.csv
    facts/store_daily.csv
    facts/member_daily.csv
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .schema import SCHEMA_SQL, SCHEMA_VERSION

REQUIRED_FILES = (
    "reference/staff.csv",
    "reference/stores.csv",
    "reference/datasets.csv",
    "reference/metric_catalog.csv",
    "reference/access_policies.csv",
    "reference/business_events.csv",
    "reference/data_versions.csv",
    "facts/store_daily.csv",
    "facts/member_daily.csv",
)

IMPORT_COLUMNS = {
    "staff": ["user_id", "display_name", "role", "department", "region_scope", "personal_clearance", "active"],
    "stores": ["store_id", "store_name", "region", "store_format", "opened_on", "active"],
    "datasets": ["dataset_id", "dataset_name", "description", "sensitivity", "source_tables", "personal_data", "owner", "active"],
    "metrics": ["metric_id", "metric_name", "definition_version", "grain", "owner", "audience_departments", "status", "valid_from", "valid_to", "calculation_key", "source_dataset_id", "definition_text", "personal_data"],
    "access_policies": ["policy_id", "dataset_id", "region", "role", "department", "purpose", "min_clearance", "decision", "policy_version", "valid_from", "valid_to", "note"],
    "business_events": ["event_id", "region", "event_type", "event_name", "start_date", "end_date", "description"],
    "data_versions": ["data_version", "seq", "coverage_start", "coverage_end", "status", "published_at", "note"],
    "store_daily": ["date", "store_id", "region", "visits", "transactions", "group_visits", "group_transactions", "staff_transactions", "gross_sales", "group_gross_sales", "return_amount", "return_count", "stockout_skus", "total_skus", "available_seq"],
    "member_daily": ["member_ref", "date", "store_id", "region", "transactions", "spend", "available_seq"],
}
INT_COLUMNS = {
    "staff": {"personal_clearance", "active"},
    "stores": {"active"},
    "datasets": {"personal_data", "active"},
    "metrics": {"personal_data"},
    "access_policies": {"min_clearance"},
    "data_versions": {"seq"},
    "store_daily": {"visits", "transactions", "group_visits", "group_transactions", "staff_transactions", "return_count", "stockout_skus", "total_skus", "available_seq"},
    "member_daily": {"transactions", "available_seq"},
}
FLOAT_COLUMNS = {
    "store_daily": {"gross_sales", "group_gross_sales", "return_amount"},
    "member_daily": {"spend"},
}
FILE_FOR_TABLE = {
    "staff": "reference/staff.csv",
    "stores": "reference/stores.csv",
    "datasets": "reference/datasets.csv",
    "metrics": "reference/metric_catalog.csv",
    "access_policies": "reference/access_policies.csv",
    "business_events": "reference/business_events.csv",
    "data_versions": "reference/data_versions.csv",
    "store_daily": "facts/store_daily.csv",
    "member_daily": "facts/member_daily.csv",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def read_rows(path: Path, columns: list[str], int_columns: set[str], float_columns: set[str]) -> list[tuple]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != columns:
            raise ValueError(f"{path.name} 字段不匹配：期望 {columns}，实际 {reader.fieldnames}")
        rows = []
        for line_number, row in enumerate(reader, start=2):
            if None in row or any(row[name] is None for name in columns):
                raise ValueError(f"{path.name} 第 {line_number} 行列数异常")
            values = []
            for name in columns:
                raw = row[name].strip()
                if name in int_columns:
                    values.append(int(raw))
                elif name in float_columns:
                    values.append(float(raw))
                else:
                    values.append(raw)
            rows.append(tuple(values))
    return rows


def _table_empty(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def initialize(conn: sqlite3.Connection, materials_dir: str | Path, *, reset: bool = False) -> dict:
    materials = Path(materials_dir)
    missing = [name for name in REQUIRED_FILES if not (materials / name).is_file()]
    if missing:
        raise FileNotFoundError(f"缺少合成资料：{', '.join(missing)}")

    if reset:
        for table in (
            "audit_events", "actions", "reviews", "analysis_impacts", "analyses",
            "access_decisions", "access_requests", "requests", "member_daily",
            "store_daily", "data_versions", "business_events", "access_policies",
            "metrics", "datasets", "stores", "staff", "id_sequences", "meta",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()

    conn.executescript(SCHEMA_SQL)
    conn.commit()

    seeded = conn.execute("SELECT value FROM meta WHERE key = 'seeded_at'").fetchone()
    if seeded and not reset:
        return {
            "initialized": False,
            "reason": "资料已导入；使用 --reset 重新初始化",
            "schema_version": SCHEMA_VERSION,
            "seeded_at": seeded["value"],
        }

    loaded: dict[str, int] = {}
    with conn:
        for table, columns in IMPORT_COLUMNS.items():
            if not _table_empty(conn, table):
                continue
            path = materials / FILE_FOR_TABLE[table]
            rows = read_rows(path, columns, INT_COLUMNS.get(table, set()), FLOAT_COLUMNS.get(table, set()))
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                rows,
            )
            loaded[table] = len(rows)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_at', ?)",
            (now_utc(),),
        )
    return {
        "initialized": True,
        "schema_version": SCHEMA_VERSION,
        "loaded": loaded,
        "seeded_at": now_utc(),
    }
