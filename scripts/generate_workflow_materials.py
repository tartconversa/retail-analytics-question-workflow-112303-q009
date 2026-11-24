"""生成确定性的本地合成零售经营资料。

资料完全虚构，不对应真实个人、企业或交易。生成器保留在本仓库，便于复核
`data/workflow` 下的文件；应用运行不依赖本脚本。

用法：
    python scripts/generate_workflow_materials.py
"""

from __future__ import annotations

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "workflow"
START = date(2026, 8, 1)
END = date(2026, 11, 23)
LATE_STORES = {"N-301", "N-303"}
LATE_DATES = {date(2026, 11, 10) + timedelta(days=i) for i in range(7)}

# store_id, name, region, format, base_visits, base_conversion, avg_ticket, return_rate
STORES = [
    ("S-101", "城南店", "R-S", "standard", 860, 0.222, 68.0, 0.041),
    ("S-102", "湖畔店", "R-S", "compact", 620, 0.205, 61.0, 0.037),
    ("S-103", "北站店", "R-S", "flagship", 1180, 0.238, 79.0, 0.045),
    ("S-104", "老城店", "R-S", "compact", 560, 0.196, 55.0, 0.052),
    ("W-201", "西园店", "R-W", "standard", 790, 0.213, 66.0, 0.039),
    ("W-202", "机场店", "R-W", "compact", 700, 0.201, 63.0, 0.044),
    ("W-203", "高新店", "R-W", "flagship", 1090, 0.231, 82.0, 0.043),
    ("W-204", "滨河店", "R-W", "standard", 830, 0.218, 70.0, 0.040),
    ("N-301", "北苑店", "R-N", "standard", 740, 0.209, 64.0, 0.038),
    ("N-302", "学府店", "R-N", "compact", 580, 0.215, 58.0, 0.035),
    ("N-303", "科技园店", "R-N", "flagship", 1020, 0.234, 80.0, 0.042),
    ("N-304", "河湾店", "R-N", "standard", 800, 0.211, 67.0, 0.039),
]
CAMPAIGNS = {
    "R-N": (date(2026, 10, 5), date(2026, 10, 11), "金秋会员周"),
    "R-S": (date(2026, 10, 12), date(2026, 10, 18), "南区周年庆"),
    "R-W": (date(2026, 10, 19), date(2026, 10, 25), "西区家庭日"),
}
ROADWORKS = (date(2026, 11, 2), date(2026, 11, 20))
STOCK_EVENT = (date(2026, 10, 20), date(2026, 11, 5))
RETURN_EVENT = (date(2026, 10, 26), date(2026, 11, 8))
DOW = {0: 0.96, 1: 0.94, 2: 0.95, 3: 1.00, 4: 1.06, 5: 1.18, 6: 1.14}


def write_csv(relative: str, columns: list[str], rows: list[dict]) -> None:
    path = OUT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path.relative_to(ROOT)}: {len(rows)} rows")


def in_range(day: date, window: tuple[date, date]) -> bool:
    return window[0] <= day <= window[1]


def data_seq(day: date, store_id: str) -> int:
    if day <= date(2026, 11, 9):
        return 1
    if day <= date(2026, 11, 16):
        return 3 if store_id in LATE_STORES else 2
    return 3


def generate_store_daily() -> list[dict]:
    rng = random.Random(20261124)
    rows: list[dict] = []
    total_skus = 120
    current = START
    while current <= END:
        month_trend = {8: 1.0, 9: 0.98, 10: 1.03, 11: 1.0}[current.month]
        for store_id, _name, region, _fmt, visits0, conv0, ticket, ret0 in STORES:
            campaign = in_range(current, CAMPAIGNS[region][:2])
            visits = visits0 * DOW[current.weekday()] * month_trend
            visits *= 1 + 0.02 * math.sin((current - START).days / 9.0)
            visits *= 1 + rng.uniform(-0.035, 0.035)
            conversion = conv0
            if campaign:
                visits *= 1.12
                conversion *= 1.05
            if region == "R-N" and store_id in LATE_STORES and in_range(current, ROADWORKS):
                visits *= 0.80
                conversion *= 0.88
            visits_i = max(50, round(visits))
            transactions = max(1, round(visits_i * conversion))
            ticket_i = ticket * (1 + 0.015 * math.sin((current - START).days / 21.0))
            ticket_i *= 1 + rng.uniform(-0.02, 0.02)
            gross = round(transactions * ticket_i, 2)
            return_rate = ret0
            if region == "R-W" and in_range(current, RETURN_EVENT):
                return_rate *= 1.9
            return_count = round(transactions * return_rate)
            return_amount = round(return_count * ticket_i * rng.uniform(0.86, 0.94), 2)
            # “顾客口径”先从总到店/成交中剔除员工代客下单，再按顾客转化率微调；
            # 因此它通常略高于含员工口径，而不会出现翻倍的不合理值。
            staff_visits = round(visits_i * 0.012)
            staff_transactions = round(staff_visits * 0.7)
            group_visits = max(1, visits_i - staff_visits)
            group_transactions = max(0, transactions - staff_transactions)
            group_gross_sales = round(group_transactions * ticket_i * 2.0, 2)
            stockout = rng.randint(2, 6)
            if region == "R-S" and in_range(current, STOCK_EVENT):
                stockout += rng.randint(15, 22)
            if current.weekday() >= 5:
                stockout += 1
            rows.append(
                {
                    "date": current.isoformat(),
                    "store_id": store_id,
                    "region": region,
                    "visits": visits_i,
                    "transactions": transactions,
                    "group_visits": group_visits,
                    "group_transactions": group_transactions,
                    "staff_transactions": staff_transactions,
                    "gross_sales": f"{gross:.2f}",
                    "group_gross_sales": f"{group_gross_sales:.2f}",
                    "return_amount": f"{return_amount:.2f}",
                    "return_count": return_count,
                    "stockout_skus": min(total_skus, stockout),
                    "total_skus": total_skus,
                    "available_seq": data_seq(current, store_id),
                }
            )
        current += timedelta(days=1)
    return rows


def generate_member_daily() -> list[dict]:
    rng = random.Random(20261125)
    rows: list[dict] = []
    current = START
    while current <= END:
        for store_id, _name, region, _fmt, visits0, conv0, ticket, _ret in STORES:
            member_base = 12
            for index in range(member_base):
                if rng.random() > 0.52:
                    continue
                campaign = in_range(current, CAMPAIGNS[region][:2])
                roadworks = region == "R-N" and store_id in LATE_STORES and in_range(current, ROADWORKS)
                probability = 0.52
                if campaign:
                    probability += 0.08
                if roadworks:
                    probability -= 0.12
                if rng.random() > probability:
                    continue
                spend = ticket * rng.uniform(0.45, 1.8)
                rows.append(
                    {
                        "member_ref": f"MB-{store_id[2:]}-{index + 1:02d}",
                        "date": current.isoformat(),
                        "store_id": store_id,
                        "region": region,
                        "transactions": 1,
                        "spend": f"{spend:.2f}",
                        "available_seq": data_seq(current, store_id),
                    }
                )
        current += timedelta(days=1)
    return rows


def main() -> None:
    staff = [
        ("U-101", "林岚", "store_manager", "store-ops", "R-N", 1),
        ("U-102", "周远", "store_manager", "merchandising", "R-S", 0),
        ("U-103", "郑敏", "store_manager", "finance", "R-W", 0),
        ("U-104", "何川", "store_manager", "store-ops", "R-S", 0),
        ("U-105", "苏婉", "store_manager", "merchandising", "R-N", 1),
        ("U-106", "秦岚", "store_manager", "finance", "R-W", 1),
        ("AN-101", "江宁", "analyst", "data", "ALL", 0),
        ("AN-102", "沈舟", "analyst", "data", "ALL", 0),
        ("GOV-101", "顾言", "data_governor", "data", "ALL", 1),
        ("GOV-102", "唐宁", "data_governor", "data", "ALL", 1),
        ("MGR-101", "王玮", "business_owner", "store-ops", "R-N", 0),
        ("MGR-102", "陈立", "business_owner", "merchandising", "R-S", 0),
        ("MGR-103", "许清", "business_owner", "finance", "R-W", 0),
        ("MGR-104", "罗成", "business_owner", "store-ops", "R-S", 0),
        ("MGR-105", "韩梅", "business_owner", "merchandising", "R-N", 0),
        ("MGR-106", "赵庭", "business_owner", "store-ops", "R-N", 0),
    ]
    write_csv(
        "reference/staff.csv",
        ["user_id", "display_name", "role", "department", "region_scope", "personal_clearance", "active"],
        [
            {
                "user_id": user_id,
                "display_name": name,
                "role": role,
                "department": department,
                "region_scope": region,
                "personal_clearance": clearance,
                "active": 1,
            }
            for user_id, name, role, department, region, clearance in staff
        ],
    )
    write_csv(
        "reference/stores.csv",
        ["store_id", "store_name", "region", "store_format", "opened_on", "active"],
        [
            {
                "store_id": store_id,
                "store_name": name,
                "region": region,
                "store_format": fmt,
                "opened_on": "2024-03-01",
                "active": 1,
            }
            for store_id, name, region, fmt, *_rest in STORES
        ],
    )
    write_csv(
        "reference/business_events.csv",
        ["event_id", "region", "event_type", "event_name", "start_date", "end_date", "description"],
        [
            {"event_id": "EV-N-CAMPAIGN", "region": "R-N", "event_type": "campaign", "event_name": "金秋会员周", "start_date": "2026-10-05", "end_date": "2026-10-11", "description": "北区会员积分翻倍与满减活动，覆盖全部北区门店。"},
            {"event_id": "EV-S-CAMPAIGN", "region": "R-S", "event_type": "campaign", "event_name": "南区周年庆", "start_date": "2026-10-12", "end_date": "2026-10-18", "description": "南区周年庆主题活动，含线上线下联合券。"},
            {"event_id": "EV-W-CAMPAIGN", "region": "R-W", "event_type": "campaign", "event_name": "西区家庭日", "start_date": "2026-10-19", "end_date": "2026-10-25", "description": "西区家庭客群主题活动。"},
            {"event_id": "EV-N-ROADWORKS", "region": "R-N", "event_type": "traffic-disruption", "event_name": "北区地铁施工", "start_date": "2026-11-02", "end_date": "2026-11-20", "description": "北苑站与科技园店周边道路封闭施工，公交线路临时调整。"},
            {"event_id": "EV-S-STOCK", "region": "R-S", "event_type": "supply-delay", "event_name": "南区仓配延迟", "start_date": "2026-10-20", "end_date": "2026-11-05", "description": "区域仓拣货能力下降，部分门店补货延迟。"},
            {"event_id": "EV-W-RETURN", "region": "R-W", "event_type": "return-wave", "event_name": "西区尺码争议退货", "start_date": "2026-10-26", "end_date": "2026-11-08", "description": "一批次服装尺码标注争议引发集中退货。"},
        ],
    )
    write_csv(
        "reference/data_versions.csv",
        ["data_version", "seq", "coverage_start", "coverage_end", "status", "published_at", "note"],
        [
            {"data_version": "DV-2026-11-10", "seq": 1, "coverage_start": START.isoformat(), "coverage_end": "2026-11-09", "status": "published", "published_at": "2026-11-10T04:00:00Z", "note": "常规周批。"},
            {"data_version": "DV-2026-11-17", "seq": 2, "coverage_start": START.isoformat(), "coverage_end": "2026-11-16", "status": "published", "published_at": "2026-11-17T04:00:00Z", "note": "常规周批；北区两家门店 11-10 至 11-16 数据迟到。"},
            {"data_version": "DV-2026-11-24", "seq": 3, "coverage_start": START.isoformat(), "coverage_end": END.isoformat(), "status": "ready", "published_at": "", "note": "补齐北区两家门店延迟数据，并新增 11-17 至 11-23 数据。"},
        ],
    )
    write_csv(
        "reference/metric_catalog.csv",
        [
            "metric_id", "metric_name", "definition_version", "grain", "owner",
            "audience_departments", "status", "valid_from", "valid_to",
            "calculation_key", "source_dataset_id", "definition_text", "personal_data",
        ],
        [
            {"metric_id": "M-SALES-FIN", "metric_name": "net-sales", "definition_version": "DEF-1", "grain": "store-day", "owner": "finance", "audience_departments": "finance;store-ops", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "sales_net", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "销售额=含税成交金额减退货金额；财务与门店运营用于经营复盘。", "personal_data": 0},
            {"metric_id": "M-SALES-MERCH", "metric_name": "net-sales", "definition_version": "DEF-2", "grain": "store-day", "owner": "merchandising", "audience_departments": "merchandising", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "sales_gross", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "销售额=商品成交毛额，不做退货冲减；商品部用于品类复盘。", "personal_data": 0},
            {"metric_id": "M-SALES-LEGACY", "metric_name": "net-sales", "definition_version": "DEF-0", "grain": "sku-week", "owner": "operations", "audience_departments": "store-ops", "status": "retired", "valid_from": "2025-01-01", "valid_to": "2025-12-31", "calculation_key": "sales_gross", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "旧口径，已停用，不能用于新结论。", "personal_data": 0},
            {"metric_id": "M-CONV-OPS", "metric_name": "conversion", "definition_version": "DEF-1", "grain": "store-day", "owner": "store-ops", "audience_departments": "store-ops;finance", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "conversion_store", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "到店转化率=成交笔数/到店人次，包含员工代客下单。", "personal_data": 0},
            {"metric_id": "M-CONV-MERCH", "metric_name": "conversion", "definition_version": "DEF-2", "grain": "store-day", "owner": "merchandising", "audience_departments": "merchandising", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "conversion_group", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "顾客到店转化率=顾客成交笔数/顾客到店人次，剔除员工代客下单。", "personal_data": 0},
            {"metric_id": "M-CONV-REVIEW", "metric_name": "conversion", "definition_version": "DEF-3", "grain": "store-day", "owner": "data", "audience_departments": "store-ops", "status": "review", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "conversion_group", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "候选口径，等待数据委员会确认，暂不可交付。", "personal_data": 0},
            {"metric_id": "M-AVAIL-OPS", "metric_name": "availability", "definition_version": "DEF-1", "grain": "store-day", "owner": "store-ops", "audience_departments": "store-ops;data", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "availability", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "有货率=1-缺货SKU数/在售SKU数，按门店日汇总。", "personal_data": 0},
            {"metric_id": "M-RETURN-FIN", "metric_name": "return-rate", "definition_version": "DEF-1", "grain": "store-day", "owner": "finance", "audience_departments": "finance;merchandising", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "return_rate", "source_dataset_id": "DS-STORE-DAILY", "definition_text": "退货率=退货笔数/成交笔数。", "personal_data": 0},
            {"metric_id": "M-MEMBER-TXN", "metric_name": "member-visits", "definition_version": "DEF-1", "grain": "store-day", "owner": "data", "audience_departments": "finance;merchandising", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "member_visits", "source_dataset_id": "DS-MEMBER-DAILY", "definition_text": "会员到店笔数，按门店日汇总；明细受个人数据授权约束，交付时只展示聚合值。", "personal_data": 1},
            {"metric_id": "M-MEMBER-OPS", "metric_name": "member-visits", "definition_version": "DEF-1", "grain": "store-day", "owner": "store-ops", "audience_departments": "store-ops", "status": "active", "valid_from": "2026-08-01", "valid_to": "", "calculation_key": "member_visits", "source_dataset_id": "DS-MEMBER-DAILY", "definition_text": "会员到店笔数，按门店日汇总；仅供门店会员跟进，输出必须为聚合值且小样本受抑制。", "personal_data": 1},
        ],
    )
    write_csv(
        "reference/access_policies.csv",
        [
            "policy_id", "dataset_id", "region", "role", "department", "purpose",
            "min_clearance", "decision", "policy_version", "valid_from", "valid_to", "note",
        ],
        [
            {"policy_id": "POL-SD-DIAG", "dataset_id": "DS-STORE-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "diagnosis", "min_clearance": 0, "decision": "allow", "policy_version": "POL-2026-03", "valid_from": "2026-01-01", "valid_to": "", "note": "经营诊断可读取汇总门店日数据。"},
            {"policy_id": "POL-SD-PLAN", "dataset_id": "DS-STORE-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "planning", "min_clearance": 0, "decision": "allow", "policy_version": "POL-2026-03", "valid_from": "2026-01-01", "valid_to": "", "note": "经营计划可使用汇总门店日数据。"},
            {"policy_id": "POL-SD-COST", "dataset_id": "DS-STORE-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "cost-review", "min_clearance": 0, "decision": "allow", "policy_version": "POL-2026-03", "valid_from": "2026-01-01", "valid_to": "", "note": "成本复盘可使用汇总门店日数据。"},
            {"policy_id": "POL-SD-MEMBER", "dataset_id": "DS-STORE-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "member-followup", "min_clearance": 0, "decision": "deny", "policy_version": "POL-2026-03", "valid_from": "2026-01-01", "valid_to": "", "note": "会员跟进必须使用个人数据集，不能用门店汇总数据替代。"},
            {"policy_id": "POL-MD-MGR", "dataset_id": "DS-MEMBER-DAILY", "region": "*", "role": "store_manager", "department": "*", "purpose": "member-followup", "min_clearance": 1, "decision": "limit", "policy_version": "POL-2026-04", "valid_from": "2026-01-01", "valid_to": "", "note": "仅有个人数据许可的门店负责人可申请，且只能使用经治理批准的门店聚合结果。"},
            {"policy_id": "POL-MD-OTHER", "dataset_id": "DS-MEMBER-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "member-followup", "min_clearance": 0, "decision": "deny", "policy_version": "POL-2026-04", "valid_from": "2026-01-01", "valid_to": "", "note": "其他人员不得申请个人数据。"},
            {"policy_id": "POL-MD-DIAG", "dataset_id": "DS-MEMBER-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "diagnosis", "min_clearance": 0, "decision": "deny", "policy_version": "POL-2026-04", "valid_from": "2026-01-01", "valid_to": "", "note": "一般经营诊断不得使用个人明细。"},
            {"policy_id": "POL-MD-PLAN", "dataset_id": "DS-MEMBER-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "planning", "min_clearance": 0, "decision": "deny", "policy_version": "POL-2026-04", "valid_from": "2026-01-01", "valid_to": "", "note": "计划场景不得使用个人明细。"},
            {"policy_id": "POL-MD-COST", "dataset_id": "DS-MEMBER-DAILY", "region": "*", "role": "*", "department": "*", "purpose": "cost-review", "min_clearance": 0, "decision": "deny", "policy_version": "POL-2026-04", "valid_from": "2026-01-01", "valid_to": "", "note": "成本复盘不得使用个人明细。"},
            {"policy_id": "POL-FL-FIN", "dataset_id": "DS-FINANCE-LEDGER", "region": "*", "role": "store_manager", "department": "finance", "purpose": "cost-review", "min_clearance": 0, "decision": "limit", "policy_version": "POL-2026-02", "valid_from": "2026-01-01", "valid_to": "", "note": "财务门店负责人仅可在成本复盘中限定使用总账摘录。"},
            {"policy_id": "POL-FL-DENY", "dataset_id": "DS-FINANCE-LEDGER", "region": "*", "role": "*", "department": "*", "purpose": "*", "min_clearance": 0, "decision": "deny", "policy_version": "POL-2026-02", "valid_from": "2026-01-01", "valid_to": "", "note": "其他人员与用途不得读取财务总账。"},
        ],
    )
    write_csv(
        "reference/datasets.csv",
        ["dataset_id", "dataset_name", "description", "sensitivity", "source_tables", "personal_data", "owner", "active"],
        [
            {"dataset_id": "DS-STORE-DAILY", "dataset_name": "门店日汇总", "description": "按门店与日期汇总的到店、成交、销售、退货、缺货数据。", "sensitivity": "internal", "source_tables": "store_daily", "personal_data": 0, "owner": "data", "active": 1},
            {"dataset_id": "DS-MEMBER-DAILY", "dataset_name": "会员日明细", "description": "会员脱敏标识级别的到店消费明细，仅用于会员跟进场景。", "sensitivity": "personal", "source_tables": "member_daily", "personal_data": 1, "owner": "data", "active": 1},
            {"dataset_id": "DS-FINANCE-LEDGER", "dataset_name": "财务总账摘录", "description": "成本与费用科目摘录，仅限成本复盘；当前未接入本应用的事实表。", "sensitivity": "restricted", "source_tables": "", "personal_data": 0, "owner": "finance", "active": 1},
        ],
    )
    write_csv(
        "facts/store_daily.csv",
        ["date", "store_id", "region", "visits", "transactions", "group_visits", "group_transactions", "staff_transactions", "gross_sales", "group_gross_sales", "return_amount", "return_count", "stockout_skus", "total_skus", "available_seq"],
        generate_store_daily(),
    )
    write_csv(
        "facts/member_daily.csv",
        ["member_ref", "date", "store_id", "region", "transactions", "spend", "available_seq"],
        generate_member_daily(),
    )


if __name__ == "__main__":
    main()
