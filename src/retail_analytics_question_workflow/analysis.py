"""确定性经营分析计算。

分析引擎只读取已导入的合成事实表，不访问网络。所有结果都带口径快照、
数据版本、覆盖范围和门店明细；覆盖不完整时显式标记，绝不把缺失当零。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from .errors import ValidationError

METRIC_LABELS = {
    "sales_net": "净销售额",
    "sales_gross": "销售额",
    "conversion_store": "到店转化率",
    "conversion_group": "顾客到店转化率",
    "availability": "有货率",
    "return_rate": "退货率",
    "member_visits": "会员到店笔数",
}
RATIO_METRICS = {"conversion_store", "conversion_group", "availability", "return_rate"}
MONEY_METRICS = {"sales_net", "sales_gross"}
SUPPRESSION_THRESHOLD = 10
COVERAGE_STATES = {"complete", "partial", "late-data", "outside-coverage", "no-data"}


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def latest_published_version(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT * FROM data_versions WHERE status = 'published' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValidationError("没有已发布的合成数据版本")
    return dict(row)


def find_version(conn: sqlite3.Connection, data_version: str | None) -> dict:
    if not data_version:
        return latest_published_version(conn)
    row = conn.execute("SELECT * FROM data_versions WHERE data_version = ?", (data_version,)).fetchone()
    if row is None:
        raise ValidationError(f"未知数据版本：{data_version}")
    if row["status"] != "published":
        raise ValidationError(f"数据版本 {data_version} 尚未发布，不能用于交付")
    return dict(row)


def resolve_stores(conn: sqlite3.Connection, region_scope: str, store_ids: list[str] | None) -> list[dict]:
    if store_ids:
        placeholders = ", ".join("?" for _ in store_ids)
        rows = conn.execute(
            f"SELECT * FROM stores WHERE store_id IN ({placeholders}) ORDER BY store_id", store_ids
        ).fetchall()
        stores = [dict(row) for row in rows]
        missing = sorted(set(store_ids) - {row["store_id"] for row in stores})
        if missing:
            raise ValidationError(f"未知门店：{', '.join(missing)}")
        outside = [row["store_id"] for row in stores if row["region"] != region_scope]
        if outside:
            raise ValidationError(f"门店不在请求地区 {region_scope} 内：{', '.join(outside)}")
        return stores
    rows = conn.execute(
        "SELECT * FROM stores WHERE region = ? AND active = 1 ORDER BY store_id", (region_scope,)
    ).fetchall()
    return [dict(row) for row in rows]


def _date_list(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _coverage(
    conn: sqlite3.Connection,
    table: str,
    stores: list[dict],
    start: date,
    end: date,
    version_seq: int,
) -> dict:
    expected_days = (end - start).days + 1
    expected = expected_days * len(stores)
    store_ids = [store["store_id"] for store in stores]
    if not store_ids:
        return {
            "state": "no-data",
            "expected_rows": 0,
            "covered_rows": 0,
            "missing_rows": 0,
            "missing_ranges": [],
            "note": "所选地区没有在营门店",
        }
    placeholders = ", ".join("?" for _ in store_ids)
    rows = conn.execute(
        f"SELECT date, store_id FROM {table} "
        f"WHERE store_id IN ({placeholders}) AND date BETWEEN ? AND ? AND available_seq <= ?",
        (*store_ids, start.isoformat(), end.isoformat(), version_seq),
    ).fetchall()
    present = {(row["date"], row["store_id"]) for row in rows}
    missing_by_store: dict[str, list[str]] = defaultdict(list)
    missing_dates = set()
    for day in _date_list(start, end):
        for store_id in store_ids:
            if (day.isoformat(), store_id) not in present:
                missing_by_store[store_id].append(day.isoformat())
                missing_dates.add(day)

    missing = expected - len(present)
    if missing == 0:
        state = "complete"
    elif not present:
        state = "no-data"
    else:
        state = "partial"
    ranges: list[dict] = []
    for store_id, dates in sorted(missing_by_store.items()):
        dates.sort()
        range_start = previous = dates[0]
        for value in dates[1:]:
            current = parse_date(value)
            if current - parse_date(previous) == timedelta(days=1):
                previous = value
                continue
            ranges.append({"start": range_start, "end": previous, "stores": [store_id]})
            range_start = previous = value
        ranges.append({"start": range_start, "end": previous, "stores": [store_id]})

    return {
        "state": state,
        "expected_rows": expected,
        "covered_rows": len(present),
        "missing_rows": missing,
        "missing_ranges": _merge_ranges(ranges),
        "note": _coverage_note(state, version_seq),
    }


def _merge_ranges(ranges: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in ranges:
        grouped[(item["start"], item["end"])].update(item["stores"])
    return [
        {"start": start, "end": end, "stores": sorted(stores)}
        for (start, end), stores in sorted(grouped.items())
    ]


def _coverage_note(state: str, version_seq: int) -> str:
    if state == "complete":
        return "请求期内所有在营门店数据在选定版本中完整。"
    if state == "late-data":
        return "选定版本中部分日期或门店的数据迟到，结论只覆盖已到数据。"
    if state == "outside-coverage":
        return "请求期超出选定数据版本的覆盖截止日，不能解释为经营下降。"
    if state == "no-data":
        return "选定版本中没有可用于该范围的数据。"
    return "请求期内数据不完整，未覆盖部分不能视为零值。"


def _select(conn: sqlite3.Connection, metric: dict, stores: list[dict], start: date, end: date, seq: int) -> tuple[list[dict], dict]:
    """返回可用明细和覆盖信息。

    两个数据集共用同一批次到达的 `available_seq`；覆盖按门店日批次判断，
    因此会员日明细不会因为某天没有会员消费而被误判为缺失。
    """
    coverage = _coverage(conn, "store_daily", stores, start, end, seq)
    if coverage["state"] == "partial" and _period_after_coverage(conn, seq, end):
        coverage["state"] = "outside-coverage"
        coverage["note"] = _coverage_note("outside-coverage", seq)
    elif coverage["state"] == "partial" and _is_late_pattern(conn, seq, coverage):
        coverage["state"] = "late-data"
        coverage["note"] = _coverage_note("late-data", seq)
    elif coverage["state"] == "no-data" and _period_after_coverage(conn, seq, end):
        coverage["note"] = "请求期完全超出选定数据版本的覆盖截止日，没有可用于结论的数据。"
    store_ids = [store["store_id"] for store in stores]
    if not store_ids:
        return [], coverage
    table = "member_daily" if metric["calculation_key"] == "member_visits" else "store_daily"
    placeholders = ", ".join("?" for _ in store_ids)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE store_id IN ({placeholders}) "
        "AND date BETWEEN ? AND ? AND available_seq <= ? ORDER BY date, store_id",
        (*store_ids, start.isoformat(), end.isoformat(), seq),
    ).fetchall()
    return [dict(row) for row in rows], coverage


def _period_after_coverage(conn: sqlite3.Connection, seq: int, end: date) -> bool:
    row = conn.execute("SELECT coverage_end FROM data_versions WHERE seq = ?", (seq,)).fetchone()
    return bool(row) and end.isoformat() > row["coverage_end"]


def _is_late_pattern(conn: sqlite3.Connection, seq: int, coverage: dict) -> bool:
    """缺失只是数据版本截止日之后才应到、且晚于旧版本覆盖范围的日期，视为迟到。

    若缺失日期早于或等于选定版本自己声明覆盖到的日期，则属于真正的不完整，
    不能称为迟到。
    """
    if coverage["missing_rows"] == 0:
        return False
    if coverage["missing_rows"] * 2 > coverage["expected_rows"]:
        return False
    row = conn.execute("SELECT coverage_end FROM data_versions WHERE seq = ?", (seq,)).fetchone()
    if row is None:
        return False
    coverage_end = row["coverage_end"]
    if any(item["end"] > coverage_end for item in coverage["missing_ranges"]):
        return False
    stores = {store for item in coverage["missing_ranges"] for store in item["stores"]}
    return 1 <= len(stores) < max(1, coverage["expected_rows"] // 10)


def _aggregate(metric: dict, rows: list[dict]) -> dict:
    key = metric["calculation_key"]
    if not rows:
        return {"value": None, "numerator": None, "denominator": None,
                "days": 0, "stores": 0, "store_days": 0}
    days = len({row["date"] for row in rows})
    stores = len({row["store_id"] for row in rows})
    store_days = len(rows)
    if key == "sales_net":
        numerator = sum(row["gross_sales"] - row["return_amount"] for row in rows)
        denominator = store_days
        value = numerator / denominator if denominator else None
        unit = "currency/store-day"
    elif key == "sales_gross":
        numerator = sum(row["gross_sales"] for row in rows)
        denominator = store_days
        value = numerator / denominator if denominator else None
        unit = "currency/store-day"
    elif key == "conversion_store":
        numerator = sum(row["transactions"] for row in rows)
        denominator = sum(row["visits"] for row in rows)
        value = numerator / denominator if denominator else None
        unit = "ratio"
    elif key == "conversion_group":
        numerator = sum(row["group_transactions"] for row in rows)
        denominator = sum(row["group_visits"] for row in rows)
        value = numerator / denominator if denominator else None
        unit = "ratio"
    elif key == "availability":
        numerator = sum(row["total_skus"] - row["stockout_skus"] for row in rows)
        denominator = sum(row["total_skus"] for row in rows)
        value = numerator / denominator if denominator else None
        unit = "ratio"
    elif key == "return_rate":
        numerator = sum(row["return_count"] for row in rows)
        denominator = sum(row["transactions"] for row in rows)
        value = numerator / denominator if denominator else None
        unit = "ratio"
    elif key == "member_visits":
        numerator = sum(row["transactions"] for row in rows)
        denominator = store_days
        value = numerator / denominator if denominator else None
        unit = "visits/store-day"
    else:  # pragma: no cover - 资料导入时已限制
        raise ValidationError(f"不支持的计算口径：{key}")
    return {
        "value": round(value, 6) if value is not None else None,
        "numerator": round(numerator, 6),
        "denominator": round(denominator, 6),
        "days": days,
        "stores": stores,
        "store_days": store_days,
        "unit": unit,
    }


def _by_store(metric: dict, rows: list[dict], stores: list[dict], period_days: int) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["store_id"]].append(row)
    result = []
    for store in stores:
        store_rows = grouped.get(store["store_id"], [])
        stats = _aggregate(metric, store_rows)
        result.append(
            {
                "store_id": store["store_id"],
                "store_name": store["store_name"],
                "store_format": store["store_format"],
                "value": stats["value"],
                "days_covered": stats["days"],
                "days_expected": period_days,
                "suppressed": False,
            }
        )
    return result


def _change(current: float | None, baseline: float | None) -> dict:
    if current is None or baseline is None:
        return {"absolute": None, "relative": None, "direction": "unknown", "reason": "缺少可比基线或当前值"}
    absolute = current - baseline
    relative = absolute / abs(baseline) if baseline else None
    if relative is None:
        direction = "unknown"
    elif abs(relative) < 0.01:
        direction = "flat"
    else:
        direction = "up" if relative > 0 else "down"
    return {
        "absolute": round(absolute, 6),
        "relative": round(relative, 6) if relative is not None else None,
        "direction": direction,
    }


def _overlapping_events(conn: sqlite3.Connection, region: str, start: date, end: date) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM business_events WHERE region = ? AND start_date <= ? AND end_date >= ? "
        "ORDER BY start_date, event_id",
        (region, end.isoformat(), start.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def _diagnostics(metric: dict, coverage: dict, rows: list[dict]) -> list[dict]:
    notes: list[dict] = []
    if coverage["state"] != "complete":
        notes.append(
            {
                "code": "coverage-" + coverage["state"],
                "message": coverage["note"],
            }
        )
    if rows and metric["calculation_key"] in {"sales_net", "sales_gross", "conversion_store", "conversion_group"}:
        stockout_ratio = sum(row["stockout_skus"] for row in rows) / sum(row["total_skus"] for row in rows)
        if stockout_ratio >= 0.05:
            notes.append(
                {
                    "code": "availability-pressure",
                    "message": f"期间平均缺货 SKU 占比 {stockout_ratio:.1%}，可能共同影响销售与转化。",
                }
            )
    if rows and metric["calculation_key"] in {"sales_net", "return_rate"}:
        return_ratio = sum(row["return_count"] for row in rows) / max(1, sum(row["transactions"] for row in rows))
        if return_ratio >= 0.06:
            notes.append(
                {
                    "code": "return-pressure",
                    "message": f"期间退货笔数占比 {return_ratio:.1%}，对净销售口径影响较大。",
                }
            )
    return notes


def _suppress_member_stores(metric: dict, store_rows: list[dict], rows: list[dict]) -> None:
    if metric["calculation_key"] != "member_visits":
        return
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["store_id"]] += 1
    for item in store_rows:
        if counts.get(item["store_id"], 0) < SUPPRESSION_THRESHOLD:
            item["value"] = None
            item["suppressed"] = True


def compute(
    conn: sqlite3.Connection,
    *,
    metric: dict,
    region_scope: str,
    stores: list[dict],
    start: date,
    end: date,
    version: dict,
    comparison: str = "previous-period",
    purpose: str = "diagnosis",
) -> dict:
    if start > end:
        raise ValidationError("period_start 不能晚于 period_end")
    rows, coverage = _select(conn, metric, stores, start, end, int(version["seq"]))
    current = _aggregate(metric, rows)
    period_days = (end - start).days + 1
    store_rows = _by_store(metric, rows, stores, period_days)
    _suppress_member_stores(metric, store_rows, rows)

    baseline = None
    baseline_rows: list[dict] = []
    baseline_coverage = None
    if comparison == "previous-period":
        baseline_end = start - timedelta(days=1)
        baseline_start = baseline_end - timedelta(days=period_days - 1)
        baseline_rows, baseline_coverage = _select(
            conn, metric, stores, baseline_start, baseline_end, int(version["seq"])
        )
        baseline = _aggregate(metric, baseline_rows)
        baseline_store_rows = _by_store(metric, baseline_rows, stores, period_days)
        _suppress_member_stores(metric, baseline_store_rows, baseline_rows)
        baseline_by_store = {item["store_id"]: item for item in baseline_store_rows}
        for item in store_rows:
            base = baseline_by_store.get(item["store_id"], {})
            item["baseline"] = base.get("value")
        for item in store_rows:
            item["change"] = _change(item["value"], item.get("baseline"))
    elif comparison == "none":
        for item in store_rows:
            item["baseline"] = None
            item["change"] = _change(item["value"], None)
    else:
        raise ValidationError(f"不支持的对比方式：{comparison}")

    events = _overlapping_events(conn, region_scope, start, end)

    if metric["calculation_key"] == "member_visits" and current["value"] is not None:
        member_count = len(rows)
        if member_count < SUPPRESSION_THRESHOLD:
            current = dict(current)
            current["value"] = None
            current["suppressed"] = True
            current["suppression_threshold"] = SUPPRESSION_THRESHOLD

    result = {
        "metric": {
            "metric_id": metric["metric_id"],
            "metric_name": metric["metric_name"],
            "label": METRIC_LABELS.get(metric["calculation_key"], metric["metric_name"]),
            "definition_version": metric["definition_version"],
            "definition_text": metric["definition_text"],
            "grain": metric["grain"],
            "owner": metric["owner"],
            "audience_departments": metric["audience_departments"].split(";"),
            "source_dataset_id": metric["source_dataset_id"],
            "calculation_key": metric["calculation_key"],
            "personal_data": bool(metric["personal_data"]),
        },
        "scope": {
            "region": region_scope,
            "stores": [store["store_id"] for store in stores],
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "purpose": purpose,
        },
        "comparison": comparison,
        "value": current,
        "baseline": baseline,
        "change": _change(current.get("value"), baseline.get("value") if baseline else None),
        "stores": store_rows,
        "events": events,
        "diagnostics": _diagnostics(metric, coverage, rows),
        "coverage": coverage,
        "baseline_coverage": baseline_coverage,
        "data_version": version["data_version"],
        "data_version_seq": int(version["seq"]),
        "limitations": [
            "同期对比只描述相关关系，不能单独证明因果。",
            "缺失、迟到或未覆盖数据不会按零值处理。",
            "结论不包含价格、采购或人员调整建议；相关行动必须由业务负责人另行决定。",
        ],
        "row_count": len(rows),
    }
    if baseline_coverage and baseline_coverage["state"] != "complete":
        result["diagnostics"].append(
            {
                "code": "baseline-incomplete",
                "message": "对比基线期数据不完整，变化幅度只能作方向参考。",
            }
        )
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result["content_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return result


def coverage_impact(
    conn: sqlite3.Connection,
    *,
    table: str,
    stores: list[str],
    start: date,
    end: date,
    old_seq: int,
    new_seq: int,
) -> dict | None:
    """比较两个数据版本，返回新版本补上的缺失范围。"""
    if not stores or new_seq <= old_seq:
        return None
    placeholders = ", ".join("?" for _ in stores)
    old_rows = conn.execute(
        f"SELECT date, store_id FROM {table} WHERE store_id IN ({placeholders}) "
        "AND date BETWEEN ? AND ? AND available_seq <= ?",
        (*stores, start.isoformat(), end.isoformat(), old_seq),
    ).fetchall()
    old_present = {(row["date"], row["store_id"]) for row in old_rows}
    new_rows = conn.execute(
        f"SELECT date, store_id FROM {table} WHERE store_id IN ({placeholders}) "
        "AND date BETWEEN ? AND ? AND available_seq <= ?",
        (*stores, start.isoformat(), end.isoformat(), new_seq),
    ).fetchall()
    filled = sorted(
        (row["date"], row["store_id"]) for row in new_rows if (row["date"], row["store_id"]) not in old_present
    )
    if not filled:
        return None
    dates = [item[0] for item in filled]
    return {
        "affected_start": min(dates),
        "affected_end": max(dates),
        "affected_stores": sorted({item[1] for item in filled}),
        "rows": len(filled),
    }
