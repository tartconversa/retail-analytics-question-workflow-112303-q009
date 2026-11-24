"""命令行入口。

示例：
    python -m retail_analytics_question_workflow init --db work.db --materials data/workflow
    python -m retail_analytics_question_workflow request-new --db work.db --as U-101 \
        --question "..." --region R-N --start 2026-10-27 --end 2026-11-23 \
        --purpose diagnosis --owner MGR-101
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import Database
from .errors import WorkflowError
from .materials import connect, initialize
from .render import request_record, request_report, run_report
from .service import WorkflowService


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retail-analytics-workflow", description="门店经营分析请求工作流")
    parser.add_argument("--db", default="workflow.db", help="SQLite 文件路径，默认 workflow.db")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化数据库并导入合成资料")
    init.add_argument("--materials", default="data/workflow")
    init.add_argument("--reset", action="store_true")

    roster = sub.add_parser("staff", help="查看人员")
    roster.add_argument("--id", dest="user_id")

    new = sub.add_parser("request-new", help="登记经营问题（门店负责人）")
    new.add_argument("--as", dest="actor", required=True)
    new.add_argument("--question", required=True)
    new.add_argument("--region", required=True)
    new.add_argument("--start", required=True)
    new.add_argument("--end", required=True)
    new.add_argument("--purpose", default="diagnosis")
    new.add_argument("--owner", required=True)

    clarify = sub.add_parser("clarify", help="补充澄清记录")
    clarify.add_argument("--as", dest="actor", required=True)
    clarify.add_argument("--request", required=True)
    clarify.add_argument("--kind", choices=("question", "answer", "note"), required=True)
    clarify.add_argument("--message", required=True)

    metric = sub.add_parser("choose-metric", help="分析人员选择口径")
    metric.add_argument("--as", dest="actor", required=True)
    metric.add_argument("--request", required=True)
    metric.add_argument("--metric", required=True)
    metric.add_argument("--clarified-question", required=True)

    access = sub.add_parser("request-access", help="申请数据访问")
    access.add_argument("--as", dest="actor", required=True)
    access.add_argument("--request", required=True)
    access.add_argument("--dataset", required=True)
    access.add_argument("--justification", required=True)

    decide = sub.add_parser("decide-access", help="治理人员决定数据访问")
    decide.add_argument("--as", dest="actor", required=True)
    decide.add_argument("--access", required=True)
    decide.add_argument("--decision", choices=("allow", "limit", "deny"), required=True)
    decide.add_argument("--justification", required=True)
    decide.add_argument("--output-scope", choices=("store-aggregate", "region-aggregate", "row-detail"), required=True)

    revoke = sub.add_parser("revoke-access", help="治理人员撤销访问")
    revoke.add_argument("--as", dest="actor", required=True)
    revoke.add_argument("--access", required=True)
    revoke.add_argument("--reason", required=True)

    preview = sub.add_parser("preview", help="只读预览分析结果，不写入")
    _analysis_options(preview)

    run = sub.add_parser("run", help="生成分析草稿")
    _analysis_options(run)

    deliver = sub.add_parser("deliver", help="交付分析")
    deliver.add_argument("--as", dest="actor", required=True)
    deliver.add_argument("--run", required=True)
    deliver.add_argument("--note", default="")
    deliver.add_argument("--acknowledge-partial", action="store_true")

    redo = sub.add_parser("reanalyze", help="用新数据版本重新分析（不改写旧结论）")
    _analysis_options(redo)

    publish = sub.add_parser("publish-version", help="发布数据版本并标注影响")
    publish.add_argument("--as", dest="actor", required=True)
    publish.add_argument("--version", required=True)

    review = sub.add_parser("review", help="业务负责人复核结论")
    review.add_argument("--as", dest="actor", required=True)
    review.add_argument("--request", required=True)
    review.add_argument("--run", required=True)
    review.add_argument("--answered", choices=("yes", "no"), required=True)
    review.add_argument("--follow-up", choices=("yes", "no"), required=True)
    review.add_argument("--note", required=True)

    propose = sub.add_parser("propose-action", help="业务负责人登记行动建议")
    propose.add_argument("--as", dest="actor", required=True)
    propose.add_argument("--request", required=True)
    propose.add_argument("--decision", required=True)
    propose.add_argument("--rationale", required=True)
    propose.add_argument("--run")

    accept = sub.add_parser("accept-action", help="业务负责人接受行动，形成责任和期限")
    accept.add_argument("--as", dest="actor", required=True)
    accept.add_argument("--action", required=True)
    accept.add_argument("--due", required=True)
    accept.add_argument("--note", default="")

    start = sub.add_parser("start-action", help="标记行动执行中")
    start.add_argument("--as", dest="actor", required=True)
    start.add_argument("--action", required=True)

    complete = sub.add_parser("complete-action", help="登记行动完成结果")
    complete.add_argument("--as", dest="actor", required=True)
    complete.add_argument("--action", required=True)
    complete.add_argument("--result-ref", required=True)
    complete.add_argument("--result-note", required=True)

    cancel = sub.add_parser("cancel-action", help="取消行动")
    cancel.add_argument("--as", dest="actor", required=True)
    cancel.add_argument("--action", required=True)
    cancel.add_argument("--reason", required=True)

    show = sub.add_parser("show", help="查看请求全貌（--json 输出可序列化记录）")
    show.add_argument("--as", dest="actor", required=True)
    show.add_argument("--request", required=True)
    show.add_argument("--json", action="store_true")

    show_run = sub.add_parser("show-run", help="查看分析结论")
    show_run.add_argument("--as", dest="actor", required=True)
    show_run.add_argument("--run", required=True)
    show_run.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", help="列出可见请求")
    listing.add_argument("--as", dest="actor", required=True)
    listing.add_argument("--state")
    listing.add_argument("--json", action="store_true")

    actions = sub.add_parser("list-actions", help="列出行动")
    actions.add_argument("--as", dest="actor", required=True)
    actions.add_argument("--state")
    actions.add_argument("--json", action="store_true")

    dashboard = sub.add_parser("dashboard", help="角色工作台摘要")
    dashboard.add_argument("--as", dest="actor", required=True)

    audit = sub.add_parser("audit", help="请求审计轨迹")
    audit.add_argument("--as", dest="actor", required=True)
    audit.add_argument("--request", required=True)

    policy = sub.add_parser("policy", help="策略评估（只读）")
    policy.add_argument("--as", dest="actor", required=True)
    policy.add_argument("--dataset", required=True)
    policy.add_argument("--purpose", required=True)
    return parser


def _analysis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--as", dest="actor", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--comparison", choices=("previous-period", "none"), default="previous-period")
    parser.add_argument("--data-version")
    parser.add_argument("--stores", help="逗号分隔的门店编号，默认整个地区")


def _store_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def dispatch(args: argparse.Namespace, service: WorkflowService) -> tuple[str, object]:
    command = args.command
    if command == "init":
        result = initialize(service.db.conn, args.materials, reset=args.reset)
        return _json(result), result
    if command == "staff":
        if args.user_id:
            return _json(service.staff_profile(args.user_id)), None
        rows = service.db.all(service.db.conn, "SELECT * FROM staff ORDER BY user_id")
        return _json(rows), rows
    if command == "request-new":
        result = service.register_request(
            args.actor, question_text=args.question, region_scope=args.region,
            period_start=args.start, period_end=args.end, purpose=args.purpose,
            decision_owner_id=args.owner,
        )
        return request_report(result), result
    if command == "clarify":
        return _json(service.add_clarification(args.actor, args.request, kind=args.kind, message=args.message)), None
    if command == "choose-metric":
        return _json(service.set_metric(args.actor, args.request, metric_id=args.metric,
                                        clarified_question=args.clarified_question)), None
    if command == "request-access":
        return _json(service.request_access(args.actor, args.request, dataset_id=args.dataset,
                                            justification=args.justification)), None
    if command == "decide-access":
        return _json(service.decide_access(args.actor, args.access, decision=args.decision,
                                           justification=args.justification, output_scope=args.output_scope)), None
    if command == "revoke-access":
        return _json(service.revoke_access(args.actor, args.access, reason=args.reason)), None
    if command == "preview":
        result = service.compute_preview(
            args.actor, args.request, comparison=args.comparison,
            data_version=args.data_version, store_ids=_store_ids(args.stores),
        )
        return run_report({"run_id": "PREVIEW", "request_id": args.request, "state": "preview",
                           "data_version": result["data_version"], "coverage": result["coverage"],
                           "content_hash": result["content_hash"], "metric": result["metric"],
                           "result": result, "impacts": []}), result
    if command == "run":
        result = service.run_analysis(
            args.actor, args.request, comparison=args.comparison,
            data_version=args.data_version, store_ids=_store_ids(args.stores),
        )
        return _json(result), result
    if command == "deliver":
        result = service.deliver_analysis(args.actor, args.run, note=args.note,
                                          acknowledge_partial=args.acknowledge_partial)
        return run_report(result), result
    if command == "reanalyze":
        result = service.reanalyze(
            args.actor, args.request, comparison=args.comparison,
            data_version=args.data_version, store_ids=_store_ids(args.stores),
        )
        return _json(result), result
    if command == "publish-version":
        return _json(service.publish_data_version(args.actor, args.version)), None
    if command == "review":
        result = service.review_delivery(
            args.actor, args.request, run_id=args.run, answered=args.answered == "yes",
            follow_up=args.follow_up == "yes", note=args.note,
        )
        return _json(result), None
    if command == "propose-action":
        return _json(service.propose_action(args.actor, args.request, decision=args.decision,
                                            rationale=args.rationale, run_id=args.run)), None
    if command == "accept-action":
        return _json(service.accept_action(args.actor, args.action, due_at=args.due, note=args.note)), None
    if command == "start-action":
        return _json(service.start_action(args.actor, args.action)), None
    if command == "complete-action":
        return _json(service.complete_action(args.actor, args.action, result_ref=args.result_ref,
                                             result_note=args.result_note)), None
    if command == "cancel-action":
        return _json(service.cancel_action(args.actor, args.action, reason=args.reason)), None
    if command == "show":
        detail = service.get_request(args.actor, args.request)
        result = request_record(detail) if args.json else detail
        return (_json(result) if args.json else request_report(detail)), result
    if command == "show-run":
        result = service.get_run(args.actor, args.run)
        return (_json(result) if args.json else run_report(result)), result
    if command == "list":
        rows = service.list_requests(args.actor, state=args.state)
        if args.json:
            return _json(rows), rows
        lines = [f"{row['request_id']}  {row['state']:<20} {row['region_scope']}  {row['question_text']}"
                 for row in rows]
        return "\n".join(lines) or "没有可见请求", rows
    if command == "list-actions":
        rows = service.list_actions(args.actor, state=args.state)
        if args.json:
            return _json(rows), rows
        lines = [
            f"{row['action_id']}  {row['state']:<10} {row['decision']:<16} "
            f"责任 {row['business_owner'] or '-':<8} 截止 {row['due_at'] or '-'}"
            for row in rows
        ]
        return "\n".join(lines) or "没有可见行动", rows
    if command == "dashboard":
        result = service.workflow_dashboard(args.actor)
        return _json(result), result
    if command == "audit":
        rows = service.audit_trail(args.actor, args.request)
        return _json(rows), rows
    if command == "policy":
        result = service.evaluate_policy(dataset_id=args.dataset, user_id=args.actor, purpose=args.purpose)
        return _json(result), result
    raise WorkflowError(f"未知命令：{command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    conn = connect(args.db)
    service = WorkflowService(Database(conn))
    try:
        text, _value = dispatch(args, service)
        print(text)
        return 0
    except WorkflowError as exc:
        print(f"错误（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
