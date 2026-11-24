"""生成一个完整的本地演示数据库。

运行：
    python scripts/seed_demo.py /tmp/demo.db

演示覆盖：登记 → 澄清 → 选口径 → 申请访问 → 治理决定 → 分析（迟到数据）→
交付 → 发布新版本并标注影响 → 重新分析 → 业务复核 → 提出/接受行动 → 登记完成。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from retail_analytics_question_workflow.db import Database  # noqa: E402
from retail_analytics_question_workflow.materials import connect, initialize  # noqa: E402
from retail_analytics_question_workflow.service import WorkflowService  # noqa: E402


def build(db_path: Path) -> dict:
    conn = connect(db_path)
    initialize(conn, ROOT / "data" / "workflow", reset=True)
    service = WorkflowService(Database(conn))
    try:
        request = service.register_request(
            "U-101",
            question_text="北区近两周到店转化为什么下降？",
            region_scope="R-N",
            period_start="2026-11-03",
            period_end="2026-11-16",
            purpose="diagnosis",
            decision_owner_id="MGR-101",
        )
        request_id = request["request"]["request_id"]
        service.add_clarification(
            "AN-101", request_id, kind="question",
            message="销售额与转化率都有多种口径，确认关注哪一种？",
        )
        service.add_clarification(
            "U-101", request_id, kind="answer",
            message="关注顾客到店转化，门店层面汇总，不包含会员明细。",
        )
        service.add_clarification(
            "MGR-101", request_id, kind="note",
            message="结论请覆盖北区全部在营门店，并说明同期经营事件。",
        )
        service.set_metric(
            "AN-101", request_id, metric_id="M-CONV-OPS",
            clarified_question="北区门店 11-03 至 11-16 顾客到店转化率较前两周变化多少？",
        )
        access = service.request_access(
            "AN-101", request_id, dataset_id="DS-STORE-DAILY",
            justification="经营诊断需要门店日汇总，不涉及个人明细。",
        )
        service.decide_access(
            "GOV-101", access["access_id"], decision="allow",
            justification="用途为经营诊断，输出门店聚合，符合 POL-2026-03。",
            output_scope="store-aggregate",
        )
        run = service.run_analysis("AN-101", request_id)
        service.deliver_analysis(
            "AN-101", run["run_id"],
            note="N-301 与 N-303 在 11-10 至 11-16 的数据迟到，结论只覆盖已到数据。",
            acknowledge_partial=True,
        )
        published = service.publish_data_version("GOV-101", "DV-2026-11-24")
        rerun = service.reanalyze("AN-101", request_id, data_version="DV-2026-11-24")
        service.deliver_analysis("AN-101", rerun["run_id"], note="补齐迟到数据后的复算版本。")
        service.review_delivery(
            "MGR-101", request_id, run_id=rerun["run_id"], answered=True, follow_up=True,
            note="下滑主要集中在施工影响的两家门店，接受结论并跟进两周。",
        )
        action = service.propose_action(
            "MGR-101", request_id, decision="observe",
            rationale="施工结束前后持续观察北苑店与科技园店客流与转化恢复情况。",
        )
        service.accept_action("MGR-101", action["action_id"], due_at="2026-12-10",
                              note="每周查看一次门店日数据。")
        service.start_action("MGR-101", action["action_id"])
        service.complete_action(
            "MGR-101", action["action_id"], result_ref="RES-DEMO-01",
            result_note="施工结束后两周转化恢复至前三个月均值附近。",
        )
        return {
            "db": str(db_path),
            "request_id": request_id,
            "delivered_run": run["run_id"],
            "reanalysis_run": rerun["run_id"],
            "published_version": published["data_version"],
            "final_state": service.get_request("MGR-101", request_id)["request"]["state"],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", nargs="?", default="demo.db")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的演示库")
    args = parser.parse_args()
    path = Path(args.db)
    if path.exists() and not args.force:
        print(f"{path} 已存在；使用 --force 覆盖", file=sys.stderr)
        return 1
    print(build(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
