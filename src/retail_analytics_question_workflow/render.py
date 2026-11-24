"""把服务层结果渲染成适合终端和评审阅读的中文文本。"""

from __future__ import annotations

from typing import Any

from .analysis import METRIC_LABELS

COVERAGE_LABELS = {
    "complete": "完整",
    "partial": "部分覆盖",
    "late-data": "数据迟到",
    "outside-coverage": "超出数据版本覆盖",
    "no-data": "无数据",
}
STATE_LABELS = {
    "new": "新登记",
    "clarifying": "澄清中",
    "ready_for_access": "待申请数据",
    "access_pending": "数据访问待审",
    "blocked": "受阻",
    "ready_for_analysis": "待分析",
    "in_analysis": "分析中",
    "delivered": "已交付",
    "closed_unanswered": "未回答经营问题",
    "accepted_no_action": "已接受结论，不跟进",
    "follow_up_pending": "待形成行动",
    "action_pending": "行动已接受",
    "action_in_progress": "行动执行中",
    "completed": "已完成",
    "cancelled": "已取消",
}
ACTION_STATE_LABELS = {
    "proposed": "建议未接受（无责任与期限）",
    "accepted": "已接受（形成责任与期限）",
    "completed": "已完成",
    "cancelled": "已取消",
}


def fmt_value(value: Any, unit: str | None = None) -> str:
    if value is None:
        return "不适用/已抑制"
    if unit == "ratio" or (isinstance(value, float) and -1 <= value <= 1 and unit is None):
        return f"{value:.2%}"
    if unit in {"currency/store-day", "currency/day", "currency"}:
        return f"{value:,.2f}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def run_report(run: dict) -> str:
    result = run.get("result") or run.get("result_summary") or {}
    metric = run.get("metric") or (result.get("metric") if isinstance(result, dict) else {}) or {}
    scope = result.get("scope", {}) if isinstance(result, dict) else {}
    coverage = run.get("coverage", {})
    lines = [
        f"分析 {run['run_id']}（请求 {run['request_id']}）",
        f"状态：{run['state']}    数据版本：{run['data_version']}    内容摘要：{run['content_hash'][:16]}…",
        f"指标：{metric.get('metric_name', '')} {metric.get('definition_version', '')}"
        f"（{metric.get('label') or METRIC_LABELS.get(metric.get('calculation_key', ''), '')}）",
        f"口径：{metric.get('definition_text', '')}",
        f"范围：{scope.get('region', '')} / {scope.get('period_start', '')} 至 {scope.get('period_end', '')}",
        f"覆盖：{COVERAGE_LABELS.get(coverage.get('state'), coverage.get('state'))}"
        f"（{coverage.get('covered_rows', 0)}/{coverage.get('expected_rows', 0)} 门店日）",
    ]
    if coverage.get("missing_ranges"):
        lines.append("缺失/迟到范围：")
        for item in coverage["missing_ranges"]:
            lines.append(f"  - {item['start']} 至 {item['end']}：{', '.join(item['stores'])}")
    value = result.get("value", {}) if isinstance(result, dict) else {}
    baseline = result.get("baseline") or {}
    change = result.get("change") or {}
    lines.append(f"当前值：{fmt_value(value.get('value'), value.get('unit'))}")
    if baseline.get("value") is not None:
        lines.append(f"对比期：{fmt_value(baseline.get('value'), baseline.get('unit'))}")
    if change.get("relative") is not None:
        lines.append(
            f"变化：{change['relative']:+.2%}（{change['absolute']:+.6f}），方向 {change['direction']}"
        )
    if result.get("stores"):
        lines.append("门店明细：")
        for store in result["stores"]:
            if store.get("suppressed"):
                shown = "小样本抑制"
            else:
                shown = fmt_value(store.get("value"), value.get("unit"))
            lines.append(
                f"  - {store['store_id']} {store['store_name']}：{shown}"
                f"（{store.get('days_covered', 0)}/{store.get('days_expected', 0)} 天）"
            )
    events = result.get("events") or []
    if events:
        lines.append("相关经营事件：")
        for event in events:
            lines.append(
                f"  - {event['start_date']} 至 {event['end_date']} {event['event_name']}（{event['event_type']}）"
                f"：{event['description']}"
            )
    diagnostics = result.get("diagnostics") or []
    if diagnostics:
        lines.append("诊断提示：")
        for note in diagnostics:
            lines.append(f"  - [{note['code']}] {note['message']}")
    if result.get("limitations"):
        lines.append("限制：")
        for item in result["limitations"]:
            lines.append(f"  - {item}")
    if run.get("impacts"):
        lines.append("后续影响：")
        for impact in run["impacts"]:
            lines.append(
                f"  - {impact['detected_at'][:10]} {impact['kind']}：{impact['affected_start']} 至 "
                f"{impact['affected_end']}，门店 {impact['affected_stores']}；{impact['detail']}"
            )
    return "\n".join(lines)


def request_record(detail: dict) -> dict:
    """把请求输出转成可 JSON 序列化的结构，剥离内部数据库字段。"""
    request = detail["request"]
    return {
        "request_id": request["request_id"],
        "state": request["state"],
        "department": request["department"],
        "region_scope": request["region_scope"],
        "period_start": request["period_start"],
        "period_end": request["period_end"],
        "purpose": request["purpose"],
        "question_text": request["question_text"],
        "clarified_question": request["clarified_question"],
        "status_reason": request["status_reason"],
        "requester": detail["requester"]["user_id"],
        "decision_owner": detail["decision_owner"]["user_id"],
        "assigned_analyst": detail["assigned_analyst"]["user_id"] if detail["assigned_analyst"] else None,
        "metric": detail["metric"],
        "clarifications": detail["clarifications"],
        "accesses": detail["accesses"],
        "runs": detail["runs"],
        "reviews": detail["reviews"],
        "actions": detail["actions"],
        "events": detail["events"],
        "audit": detail["audit"],
    }


def request_report(detail: dict) -> str:
    request = detail["request"]
    lines = [
        f"请求 {request['request_id']}    {STATE_LABELS.get(request['state'], request['state'])}",
        f"提问者：{detail['requester']['display_name']}（{request['requester_id']}，{request['department']}）"
        f"  决策负责人：{detail['decision_owner']['display_name']}（{request['decision_owner_id']}）",
        f"地区/期间：{request['region_scope']} / {request['period_start']} 至 {request['period_end']}"
        f"    用途：{request['purpose']}",
        f"原始问题：{request['question_text']}",
    ]
    if request.get("clarified_question"):
        lines.append(f"澄清后问题：{request['clarified_question']}")
    if request.get("status_reason"):
        lines.append(f"状态原因：{request['status_reason']}")
    metric = detail.get("metric")
    if metric:
        lines.append(
            f"指标：{metric['metric_name']} {metric['definition_version']}，适用部门 {metric['audience_departments']}"
            f"，数据源 {metric['source_dataset_id']}"
        )
        lines.append(f"口径：{metric['definition_text']}")
    if detail["clarifications"]:
        lines.append("澄清记录：")
        for item in detail["clarifications"]:
            lines.append(f"  - {item['created_at']} {item['author_id']} [{item['kind']}] {item['message']}")
    if detail["accesses"]:
        lines.append("数据访问：")
        for item in detail["accesses"]:
            decision = f"{item['decision']}/{item['output_scope']}" if item["decision"] else "未决定"
            lines.append(
                f"  - {item['access_id']} {item['dataset_id']} {item['purpose']}：{item['status']}，{decision}"
                f"，策略 {item['policy_version'] or '-'}"
            )
    if detail["runs"]:
        lines.append("分析交付：")
        for run in detail["runs"]:
            covered = COVERAGE_LABELS.get(run["coverage"].get("state"), run["coverage"].get("state"))
            lines.append(
                f"  - {run['run_id']} {run['state']} 版本 {run['data_version']} 覆盖 {covered}"
                f" 摘要 {run['content_hash'][:16]}…"
            )
            for impact in run["impacts"]:
                lines.append(
                    f"      影响：{impact['kind']} {impact['affected_start']} 至 {impact['affected_end']}"
                    f" {'/'.join(impact['affected_stores'].split(','))}"
                )
    if detail["reviews"]:
        lines.append("业务复核：")
        for review in detail["reviews"]:
            lines.append(
                f"  - {review['created_at']} {review['owner_id']}：回答={bool(review['answered'])}"
                f" 跟进={bool(review['follow_up'])}；{review['note']}"
            )
    if detail["actions"]:
        lines.append("行动：")
        for action in detail["actions"]:
            lines.append(
                f"  - {action['action_id']} {action['decision']}："
                f"{ACTION_STATE_LABELS.get(action['state'], action['state'])}"
                f"，责任人 {action['business_owner'] or '未指定'}，截止 {action['due_at'] or '未设定'}"
            )
    return "\n".join(lines)
