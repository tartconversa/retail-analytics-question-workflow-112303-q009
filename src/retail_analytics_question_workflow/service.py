"""经营分析请求服务。

服务层集中实现角色权限、状态机、口径选择、访问决定、分析交付和行动跟踪。
所有写入都在事务内完成，并写入 `audit_events`。命令行与测试都通过这一层操作。
"""

from __future__ import annotations

import json
import sqlite3

from . import analysis
from .db import Database
from .errors import AuthorizationError, ConflictError, NotFoundError, PolicyError, ValidationError

PURPOSES = {"diagnosis", "planning", "cost-review", "member-followup"}
OUTPUT_SCOPES = {"store-aggregate", "region-aggregate", "row-detail"}
ACTION_DECISIONS = {
    "observe",
    "investigate",
    "change-display",
    "change-assortment",
    "no-action",
}
DELIVERY_STATES = {"ready_for_analysis", "in_analysis"}
REVIEW_STATES = {"delivered"}
REOPENABLE = {"blocked", "access_pending", "ready_for_access"}


class WorkflowService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 读取

    def _staff(self, conn: sqlite3.Connection, user_id: str) -> dict:
        row = conn.execute("SELECT * FROM staff WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"未知人员：{user_id}")
        staff = dict(row)
        if not staff["active"]:
            raise AuthorizationError(f"人员 {user_id} 已停用")
        return staff

    def staff_profile(self, user_id: str) -> dict:
        with self.db.tx() as conn:
            return self._staff(conn, user_id)

    def _require_role(self, actor: dict, *roles: str) -> None:
        if actor["role"] not in roles:
            raise AuthorizationError(
                f"角色 {actor['role']} 不能执行此操作；需要 {'/'.join(roles)}"
            )

    def _request(self, conn: sqlite3.Connection, request_id: str) -> dict:
        row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"未知请求：{request_id}")
        return dict(row)

    def _run(self, conn: sqlite3.Connection, run_id: str) -> dict:
        row = conn.execute("SELECT * FROM analyses WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"未知分析：{run_id}")
        return dict(row)

    def _access(self, conn: sqlite3.Connection, access_id: str) -> dict:
        row = conn.execute("SELECT * FROM access_requests WHERE access_id = ?", (access_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"未知数据访问申请：{access_id}")
        return dict(row)

    def _can_view_request(self, actor: dict, request: dict) -> bool:
        if actor["role"] in {"analyst", "data_governor"}:
            return True
        if actor["role"] == "store_manager":
            return request["requester_id"] == actor["user_id"]
        if actor["role"] == "business_owner":
            return request["decision_owner_id"] == actor["user_id"]
        return False

    def _visible_request(self, conn: sqlite3.Connection, actor: dict, request_id: str) -> dict:
        request = self._request(conn, request_id)
        if not self._can_view_request(actor, request):
            raise AuthorizationError(f"{actor['user_id']} 不能查看请求 {request_id}")
        return request

    # ------------------------------------------------------------------ 登记请求

    def register_request(
        self,
        requester_id: str,
        *,
        question_text: str,
        region_scope: str,
        period_start: str,
        period_end: str,
        purpose: str,
        decision_owner_id: str,
    ) -> dict:
        question = question_text.strip()
        if not question:
            raise ValidationError("问题描述不能为空")
        if purpose not in PURPOSES:
            raise ValidationError(f"用途必须是：{', '.join(sorted(PURPOSES))}")
        start = analysis.parse_date(period_start)
        end = analysis.parse_date(period_end)
        if start > end:
            raise ValidationError("period_start 不能晚于 period_end")

        with self.db.tx() as conn:
            requester = self._staff(conn, requester_id)
            self._require_role(requester, "store_manager")
            if requester["region_scope"] not in {region_scope, "ALL"}:
                raise AuthorizationError(f"提问者地区权限为 {requester['region_scope']}，不能登记 {region_scope}")
            store = conn.execute(
                "SELECT COUNT(*) FROM stores WHERE region = ? AND active = 1", (region_scope,)
            ).fetchone()[0]
            if not store:
                raise ValidationError(f"地区 {region_scope} 没有在营门店")
            owner = self._staff(conn, decision_owner_id)
            self._require_role(owner, "business_owner")
            if owner["region_scope"] not in {region_scope, "ALL"}:
                raise AuthorizationError("决策负责人必须覆盖请求地区")
            request_id = self.db.next_id(conn, "request", "RQ")
            now = self.db.now()
            conn.execute(
                """
                INSERT INTO requests (
                    request_id, requester_id, decision_owner_id, department, region_scope,
                    question_text, purpose, period_start, period_end, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """,
                (
                    request_id, requester_id, decision_owner_id, requester["department"],
                    region_scope, question, purpose, start.isoformat(), end.isoformat(), now, now,
                ),
            )
            self.db.audit(
                conn, actor_id=requester_id, action="request.registered", object_type="request",
                object_id=request_id, request_id=request_id,
                detail={"region": region_scope, "purpose": purpose, "period": [start.isoformat(), end.isoformat()]},
            )
            return self.request_detail(conn, actor=requester, request_id=request_id, include_events=True)

    def add_clarification(
        self,
        actor_id: str,
        request_id: str,
        *,
        kind: str,
        message: str,
    ) -> dict:
        text = message.strip()
        if not text:
            raise ValidationError("澄清内容不能为空")
        if kind not in {"question", "answer", "note"}:
            raise ValidationError("kind 必须是 question/answer/note")
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            request = self._visible_request(conn, actor, request_id)
            allowed_states = {"new", "clarifying", "ready_for_access", "access_pending", "blocked",
                              "ready_for_analysis", "in_analysis", "delivered"}
            if request["state"] not in allowed_states:
                raise ConflictError(f"请求处于 {request['state']}，不能再记录澄清")
            if actor["role"] == "business_owner" and request["decision_owner_id"] != actor["user_id"]:
                raise AuthorizationError("只有该请求的业务负责人可以记录意见")
            clarification_id = self.db.next_id(conn, "clarification", "CL")
            now = self.db.now()
            conn.execute(
                "INSERT INTO clarifications (clarification_id, request_id, author_id, kind, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (clarification_id, request_id, actor_id, kind, text, now),
            )
            new_state = request["state"]
            if request["state"] == "new" and kind in {"question", "answer"}:
                new_state = "clarifying"
            if new_state != request["state"]:
                conn.execute(
                    "UPDATE requests SET state = ?, updated_at = ?, row_version = row_version + 1 WHERE request_id = ?",
                    (new_state, now, request_id),
                )
            self.db.audit(
                conn, actor_id=actor_id, action="request.clarification_added", object_type="request",
                object_id=request_id, request_id=request_id, detail={"kind": kind},
            )
            return {"clarification_id": clarification_id, "request_state": new_state}

    def set_metric(
        self,
        analyst_id: str,
        request_id: str,
        *,
        metric_id: str,
        clarified_question: str,
    ) -> dict:
        text = clarified_question.strip()
        if not text:
            raise ValidationError("必须写下可执行的分析问题")
        with self.db.tx() as conn:
            analyst = self._staff(conn, analyst_id)
            self._require_role(analyst, "analyst")
            request = self._request(conn, request_id)
            if request["state"] not in {"new", "clarifying", "ready_for_access", "blocked"}:
                raise ConflictError(f"请求处于 {request['state']}，不能重新选择指标")
            metric = conn.execute("SELECT * FROM metrics WHERE metric_id = ?", (metric_id,)).fetchone()
            if metric is None:
                raise NotFoundError(f"未知指标：{metric_id}")
            metric = dict(metric)
            if metric["status"] != "active":
                raise PolicyError(f"指标 {metric_id} 状态为 {metric['status']}，只有 active 口径可以交付")
            audience = metric["audience_departments"].split(";")
            if request["department"] not in audience:
                raise PolicyError(
                    f"指标 {metric_id} 的适用人群为 {', '.join(audience)}，不包含 {request['department']}；"
                    "请选择该部门认可的版本或登记口径争议"
                )
            if metric["valid_from"] > request["period_end"] or (
                metric["valid_to"] and metric["valid_to"] < request["period_start"]
            ):
                raise PolicyError(f"指标 {metric_id} 的生效期与请求期间不重叠")
            now = self.db.now()
            conn.execute(
                "UPDATE requests SET metric_id = ?, clarified_question = ?, assigned_analyst_id = ?, "
                "state = 'ready_for_access', status_reason = '', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ?",
                (metric_id, text, analyst_id, now, request_id),
            )
            self.db.audit(
                conn, actor_id=analyst_id, action="request.metric_selected", object_type="request",
                object_id=request_id, request_id=request_id,
                detail={"metric_id": metric_id, "definition_version": metric["definition_version"]},
            )
            return {
                "request_id": request_id,
                "state": "ready_for_access",
                "metric_id": metric_id,
                "definition_version": metric["definition_version"],
            }

    # ------------------------------------------------------------------ 数据访问

    def _policy_candidates(self, conn: sqlite3.Connection, *, dataset_id: str, actor: dict,
                           purpose: str, region: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM access_policies WHERE dataset_id = ? ORDER BY policy_id", (dataset_id,)
        ).fetchall()
        matches = []
        for row in rows:
            policy = dict(row)
            if policy["region"] not in {"*", region}:
                continue
            if policy["role"] not in {"*", actor["role"]}:
                continue
            if policy["department"] not in {"*", actor["department"]}:
                continue
            if policy["purpose"] not in {"*", purpose}:
                continue
            if int(policy["min_clearance"]) > int(actor["personal_clearance"]):
                continue
            matches.append(policy)
        return matches

    def evaluate_policy(self, *, dataset_id: str, user_id: str, purpose: str,
                        region: str | None = None) -> dict:
        """按提问者、用途和地区给出策略允许的最宽决定。"""
        with self.db.tx() as conn:
            actor = self._staff(conn, user_id)
            dataset = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
            if dataset is None:
                raise NotFoundError(f"未知数据集：{dataset_id}")
            if not dataset["active"]:
                raise PolicyError(f"数据集 {dataset_id} 已停用")
            scope = region or actor["region_scope"]
            candidates = self._policy_candidates(
                conn, dataset_id=dataset_id, actor=actor, purpose=purpose, region=scope
            )
            if not candidates:
                raise PolicyError(f"没有覆盖 {actor['role']}/{actor['department']}/{purpose} 的数据策略")
            order = {"allow": 0, "limit": 1, "deny": 2}
            best = sorted(candidates, key=lambda item: (order[item["decision"]], item["policy_id"]))[0]
            return {
                "dataset_id": dataset_id,
                "user_id": user_id,
                "role": actor["role"],
                "department": actor["department"],
                "purpose": purpose,
                "decision": best["decision"],
                "policy_id": best["policy_id"],
                "policy_version": best["policy_version"],
                "note": best["note"],
                "matches": [item["policy_id"] for item in candidates],
            }

    def request_access(
        self,
        actor_id: str,
        request_id: str,
        *,
        dataset_id: str,
        justification: str,
    ) -> dict:
        text = justification.strip()
        if not text:
            raise ValidationError("数据访问申请必须说明理由")
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            self._require_role(actor, "analyst")
            request = self._request(conn, request_id)
            if request["state"] not in {"ready_for_access", "access_pending", "blocked"}:
                raise ConflictError(f"请求处于 {request['state']}，不能申请数据访问")
            if not request["metric_id"]:
                raise ConflictError("必须先选择指标")
            metric = conn.execute("SELECT * FROM metrics WHERE metric_id = ?", (request["metric_id"],)).fetchone()
            if metric["source_dataset_id"] != dataset_id:
                raise PolicyError(
                    f"指标 {metric['metric_id']} 使用数据集 {metric['source_dataset_id']}，"
                    f"申请 {dataset_id} 不能支持该结论"
                )
            dataset = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
            if dataset is None or not dataset["active"]:
                raise PolicyError(f"数据集 {dataset_id} 不可用")
            existing = conn.execute(
                "SELECT access_id, status FROM access_requests WHERE request_id = ? AND dataset_id = ? "
                "AND status IN ('pending', 'granted')",
                (request_id, dataset_id),
            ).fetchone()
            if existing is not None:
                raise ConflictError(f"已存在数据访问申请 {existing['access_id']}（{existing['status']}）")
            requester = self._staff(conn, request["requester_id"])
            evaluation = self.evaluate_policy(
                dataset_id=dataset_id, user_id=requester["user_id"], purpose=request["purpose"]
            )
            if evaluation["decision"] == "deny":
                raise PolicyError(
                    f"策略 {evaluation['policy_id']} 不允许 {requester['role']}/{requester['department']} "
                    f"以 {request['purpose']} 用途使用 {dataset_id}：{evaluation['note']}"
                )
            access_id = self.db.next_id(conn, "access", "AC")
            now = self.db.now()
            conn.execute(
                "INSERT INTO access_requests (access_id, request_id, dataset_id, purpose, requested_by, "
                "requested_at, justification, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (access_id, request_id, dataset_id, request["purpose"], actor_id, now, text),
            )
            conn.execute(
                "UPDATE requests SET state = 'access_pending', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ?",
                (now, request_id),
            )
            self.db.audit(
                conn, actor_id=actor_id, action="access.requested", object_type="access_request",
                object_id=access_id, request_id=request_id,
                detail={"dataset_id": dataset_id, "policy": evaluation["policy_id"],
                        "policy_decision": evaluation["decision"]},
            )
            return {
                "access_id": access_id,
                "status": "pending",
                "policy_evaluation": evaluation,
                "requires_stricter_authorization": bool(dataset["personal_data"]),
            }

    def decide_access(
        self,
        governor_id: str,
        access_id: str,
        *,
        decision: str,
        justification: str,
        output_scope: str,
    ) -> dict:
        text = justification.strip()
        if decision not in {"allow", "limit", "deny"}:
            raise ValidationError("decision 必须是 allow/limit/deny")
        if not text:
            raise ValidationError("必须记录治理决定理由")
        if output_scope not in OUTPUT_SCOPES:
            raise ValidationError(f"output_scope 必须是 {', '.join(sorted(OUTPUT_SCOPES))}")
        with self.db.tx() as conn:
            governor = self._staff(conn, governor_id)
            self._require_role(governor, "data_governor")
            access = self._access(conn, access_id)
            if access["status"] != "pending":
                raise ConflictError(f"访问申请处于 {access['status']}，不能重复决定")
            request = self._request(conn, access["request_id"])
            dataset = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (access["dataset_id"],)).fetchone()
            requester = self._staff(conn, request["requester_id"])
            evaluation = self.evaluate_policy(
                dataset_id=access["dataset_id"], user_id=requester["user_id"], purpose=access["purpose"]
            )
            rank = {"deny": 0, "limit": 1, "allow": 2}
            if rank[decision] > rank[evaluation["decision"]]:
                raise PolicyError(
                    f"策略 {evaluation['policy_id']} 最宽只允许 {evaluation['decision']}，"
                    f"治理决定不能扩大到 {decision}"
                )
            if decision != "deny":
                if dataset["personal_data"] and not requester["personal_clearance"]:
                    raise PolicyError("提问者没有个人数据许可，治理人员不能批准个人级明细")
                if output_scope == "row-detail":
                    raise PolicyError("输出范围必须是聚合结果，不能交付行级明细")
            now = self.db.now()
            policy = conn.execute(
                "SELECT * FROM access_policies WHERE policy_id = ?", (evaluation["policy_id"],)
            ).fetchone()
            decision_id = self.db.next_id(conn, "access_decision", "AD")
            conn.execute(
                "INSERT INTO access_decisions (decision_id, access_id, governor_id, decision, justification, "
                "policy_id, policy_version, output_scope, policy_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id, access_id, governor_id, decision, text, evaluation["policy_id"],
                    evaluation["policy_version"], output_scope, policy["note"], now,
                ),
            )
            status = "denied" if decision == "deny" else "granted"
            conn.execute("UPDATE access_requests SET status = ? WHERE access_id = ?", (status, access_id))
            if status == "granted":
                conn.execute(
                    "UPDATE requests SET state = 'ready_for_analysis', status_reason = '', updated_at = ?, "
                    "row_version = row_version + 1 WHERE request_id = ? AND state IN "
                    "('ready_for_access', 'access_pending', 'blocked')",
                    (now, request["request_id"]),
                )
            else:
                conn.execute(
                    "UPDATE requests SET state = 'blocked', status_reason = ?, updated_at = ?, "
                    "row_version = row_version + 1 WHERE request_id = ? AND state IN "
                    "('ready_for_access', 'access_pending', 'blocked')",
                    (f"数据访问被拒绝：{text}", now, request["request_id"]),
                )
            self.db.audit(
                conn, actor_id=governor_id, action="access.decided", object_type="access_request",
                object_id=access_id, request_id=request["request_id"],
                detail={"decision": decision, "output_scope": output_scope,
                        "policy_id": evaluation["policy_id"], "policy_version": evaluation["policy_version"]},
            )
            return {
                "access_id": access_id,
                "status": status,
                "decision": decision,
                "output_scope": output_scope,
                "request_state": "ready_for_analysis" if status == "granted" else "blocked",
            }

    def revoke_access(self, governor_id: str, access_id: str, *, reason: str) -> dict:
        text = reason.strip()
        if not text:
            raise ValidationError("撤销数据访问必须说明原因")
        with self.db.tx() as conn:
            governor = self._staff(conn, governor_id)
            self._require_role(governor, "data_governor")
            access = self._access(conn, access_id)
            if access["status"] != "granted":
                raise ConflictError(f"只有已授予的访问可以撤销，当前为 {access['status']}")
            now = self.db.now()
            conn.execute(
                "UPDATE access_requests SET status = 'revoked', revoked_at = ?, revoked_by = ?, "
                "revoke_reason = ? WHERE access_id = ?",
                (now, governor_id, text, access_id),
            )
            runs = conn.execute(
                "SELECT * FROM analyses WHERE request_id = ? AND dataset_id = ? AND state = 'delivered'",
                (access["request_id"], access["dataset_id"]),
            ).fetchall()
            affected = []
            for run in runs:
                result = json.loads(run["result_json"])
                scope = result.get("scope", {})
                previous = conn.execute(
                    "SELECT impact_id FROM analysis_impacts WHERE run_id = ? AND kind = 'permission-revoked' "
                    "AND source_ref = ?", (run["run_id"], access_id),
                ).fetchone()
                if previous:
                    continue
                impact_id = self.db.next_id(conn, "impact", "IM")
                conn.execute(
                    "INSERT INTO analysis_impacts (impact_id, run_id, kind, detected_at, source_ref, "
                    "data_version, previous_data_version, affected_start, affected_end, affected_stores, detail) "
                    "VALUES (?, ?, 'permission-revoked', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        impact_id, run["run_id"], now, access_id, run["data_version"], run["data_version"],
                        scope.get("period_start", ""), scope.get("period_end", ""),
                        ",".join(scope.get("stores", [])), text,
                    ),
                )
                affected.append(run["run_id"])
            conn.execute(
                "UPDATE requests SET state = 'blocked', status_reason = ?, updated_at = ?, "
                "row_version = row_version + 1 WHERE request_id = ? AND state IN "
                "('ready_for_access', 'access_pending', 'ready_for_analysis', 'in_analysis')",
                (f"数据访问已撤销：{text}", now, access["request_id"]),
            )
            self.db.audit(
                conn, actor_id=governor_id, action="access.revoked", object_type="access_request",
                object_id=access_id, request_id=access["request_id"],
                detail={"reason": text, "affected_runs": affected},
            )
            return {
                "access_id": access_id,
                "status": "revoked",
                "affected_runs": affected,
                "note": "已交付结论保留原口径和数据版本，并新增受影响范围记录。",
            }

    # ------------------------------------------------------------------ 分析与交付

    def _granted_access(self, conn: sqlite3.Connection, request_id: str, dataset_id: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM access_requests WHERE request_id = ? AND dataset_id = ? AND status = 'granted' "
            "ORDER BY requested_at DESC LIMIT 1",
            (request_id, dataset_id),
        ).fetchone()
        return dict(row) if row else None

    def _latest_decision(self, conn: sqlite3.Connection, access_id: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM access_decisions WHERE access_id = ? ORDER BY created_at DESC, decision_id DESC LIMIT 1",
            (access_id,),
        ).fetchone()
        return dict(row) if row else None

    def _analysis_parameters(
        self,
        conn: sqlite3.Connection,
        request: dict,
        *,
        comparison: str,
        data_version: str | None,
        store_ids: list[str] | None,
        metric: dict,
    ) -> dict:
        version = analysis.find_version(conn, data_version)
        stores = analysis.resolve_stores(conn, request["region_scope"], store_ids)
        return {
            "version": version,
            "stores": stores,
            "start": analysis.parse_date(request["period_start"]),
            "end": analysis.parse_date(request["period_end"]),
            "comparison": comparison,
        }

    def compute_preview(
        self,
        analyst_id: str,
        request_id: str,
        *,
        comparison: str = "previous-period",
        data_version: str | None = None,
        store_ids: list[str] | None = None,
    ) -> dict:
        with self.db.tx() as conn:
            analyst = self._staff(conn, analyst_id)
            self._require_role(analyst, "analyst")
            request = self._request(conn, request_id)
            if request["state"] not in DELIVERY_STATES:
                raise ConflictError(f"请求处于 {request['state']}，尚未具备分析条件")
            if not request["metric_id"]:
                raise ConflictError("请求尚未选择指标")
            metric = dict(conn.execute("SELECT * FROM metrics WHERE metric_id = ?", (request["metric_id"],)).fetchone())
            access = self._granted_access(conn, request_id, metric["source_dataset_id"])
            if access is None:
                raise PolicyError("没有有效的数据访问批准，不能生成结论")
            decision = self._latest_decision(conn, access["access_id"])
            parameters = self._analysis_parameters(
                conn, request, comparison=comparison, data_version=data_version, store_ids=store_ids, metric=metric
            )
            result = analysis.compute(
                conn, metric=metric, region_scope=request["region_scope"],
                stores=parameters["stores"], start=parameters["start"], end=parameters["end"],
                version=parameters["version"], comparison=comparison, purpose=request["purpose"],
            )
            result["access"] = {
                "access_id": access["access_id"],
                "output_scope": decision["output_scope"] if decision else "store-aggregate",
                "decision": decision["decision"] if decision else "allow",
                "policy_version": decision["policy_version"] if decision else "",
            }
            return result

    def run_analysis(
        self,
        analyst_id: str,
        request_id: str,
        *,
        comparison: str = "previous-period",
        data_version: str | None = None,
        store_ids: list[str] | None = None,
    ) -> dict:
        with self.db.tx() as conn:
            analyst = self._staff(conn, analyst_id)
            self._require_role(analyst, "analyst")
            request = self._request(conn, request_id)
            if request["state"] not in DELIVERY_STATES:
                raise ConflictError(f"请求处于 {request['state']}，不能开始分析")
            if request["metric_id"] is None:
                raise ConflictError("请求尚未选择指标")
            if request["assigned_analyst_id"] not in {None, analyst_id}:
                raise AuthorizationError("该请求已由其他分析人员接手")
            metric = dict(conn.execute("SELECT * FROM metrics WHERE metric_id = ?", (request["metric_id"],)).fetchone())
            access = self._granted_access(conn, request_id, metric["source_dataset_id"])
            if access is None:
                raise PolicyError("没有有效的数据访问批准，不能生成结论")
            decision = self._latest_decision(conn, access["access_id"])
            if decision is None:
                raise PolicyError("访问批准缺少治理决定记录")
            parameters = self._analysis_parameters(
                conn, request, comparison=comparison, data_version=data_version, store_ids=store_ids, metric=metric
            )
            if decision["output_scope"] == "row-detail":
                raise PolicyError("应用不交付行级个人明细；请将输出范围改为聚合")
            result = analysis.compute(
                conn, metric=metric, region_scope=request["region_scope"],
                stores=parameters["stores"], start=parameters["start"], end=parameters["end"],
                version=parameters["version"], comparison=comparison, purpose=request["purpose"],
            )
            if result["coverage"]["state"] == "no-data":
                raise ConflictError("选定数据版本在请求范围内没有数据，不能形成结论")
            result["access"] = {
                "access_id": access["access_id"],
                "output_scope": decision["output_scope"],
                "decision": decision["decision"],
                "policy_version": decision["policy_version"],
            }
            metric_snapshot = {
                **metric,
                "captured_at": self.db.now(),
            }
            run_id = self.db.next_id(conn, "analysis", "RUN")
            now = self.db.now()
            conn.execute(
                """
                INSERT INTO analyses (
                    run_id, request_id, metric_id, dataset_id, data_version, purpose, output_scope,
                    parameters_json, metric_snapshot_json, result_json, coverage_json, lineage_json,
                    content_hash, state, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    run_id, request_id, metric["metric_id"], metric["source_dataset_id"],
                    parameters["version"]["data_version"], request["purpose"], decision["output_scope"],
                    json.dumps(
                        {
                            "comparison": comparison,
                            "period_start": request["period_start"],
                            "period_end": request["period_end"],
                            "stores": [store["store_id"] for store in parameters["stores"]],
                            "data_version": parameters["version"]["data_version"],
                        },
                        ensure_ascii=False, sort_keys=True,
                    ),
                    json.dumps(metric_snapshot, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    json.dumps(result["coverage"], ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {
                            "dataset_id": metric["source_dataset_id"],
                            "source_tables": conn.execute(
                                "SELECT source_tables FROM datasets WHERE dataset_id = ?",
                                (metric["source_dataset_id"],),
                            ).fetchone()[0],
                            "stores": [store["store_id"] for store in parameters["stores"]],
                            "data_version": parameters["version"]["data_version"],
                        },
                        ensure_ascii=False, sort_keys=True,
                    ),
                    result["content_hash"], analyst_id, now,
                ),
            )
            conn.execute(
                "UPDATE requests SET state = 'in_analysis', assigned_analyst_id = ?, updated_at = ?, "
                "row_version = row_version + 1 WHERE request_id = ?",
                (analyst_id, now, request_id),
            )
            self.db.audit(
                conn, actor_id=analyst_id, action="analysis.created", object_type="analysis",
                object_id=run_id, request_id=request_id,
                detail={"metric_id": metric["metric_id"], "data_version": parameters["version"]["data_version"],
                        "coverage": result["coverage"]["state"]},
            )
            return self._run_summary(conn, run_id, include_result=True)

    def deliver_analysis(
        self,
        analyst_id: str,
        run_id: str,
        *,
        note: str = "",
        acknowledge_partial: bool = False,
    ) -> dict:
        with self.db.tx() as conn:
            analyst = self._staff(conn, analyst_id)
            self._require_role(analyst, "analyst")
            run = self._run(conn, run_id)
            if run["state"] != "draft":
                raise ConflictError(f"分析 {run_id} 状态为 {run['state']}，不能交付")
            if run["created_by"] != analyst_id:
                raise AuthorizationError("只有创建该分析的人员可以交付")
            request = self._request(conn, run["request_id"])
            access = self._granted_access(conn, request["request_id"], run["dataset_id"])
            if access is None:
                raise PolicyError("数据访问已失效，不能交付")
            coverage = json.loads(run["coverage_json"])
            if coverage["state"] != "complete":
                if not acknowledge_partial:
                    raise ConflictError(
                        f"数据覆盖状态为 {coverage['state']}；请先向业务负责人说明覆盖范围，"
                        "并在交付时显式确认 acknowledge_partial"
                    )
                if not note.strip():
                    raise ValidationError("覆盖不完整时交付，必须写明影响范围说明")
            now = self.db.now()
            conn.execute(
                "UPDATE analyses SET state = 'delivered', delivered_at = ?, delivery_note = ?, "
                "acknowledged_partial = ? WHERE run_id = ?",
                (now, note.strip(), 1 if acknowledge_partial else 0, run_id),
            )
            conn.execute(
                "UPDATE requests SET state = 'delivered', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ?",
                (now, request["request_id"]),
            )
            self.db.audit(
                conn, actor_id=analyst_id, action="analysis.delivered", object_type="analysis",
                object_id=run_id, request_id=request["request_id"],
                detail={"coverage": coverage["state"], "acknowledged_partial": acknowledge_partial},
            )
            return self._run_summary(conn, run_id, include_result=True)

    def reanalyze(
        self,
        analyst_id: str,
        request_id: str,
        *,
        comparison: str = "previous-period",
        data_version: str | None = None,
        store_ids: list[str] | None = None,
    ) -> dict:
        """在数据版本更新或补齐后，基于原请求生成新的分析，不改写旧结论。"""
        with self.db.tx() as conn:
            analyst = self._staff(conn, analyst_id)
            self._require_role(analyst, "analyst")
            request = self._request(conn, request_id)
            if request["state"] not in {"delivered", "completed", "accepted_no_action",
                                        "follow_up_pending", "action_pending", "action_in_progress"}:
                raise ConflictError(f"请求处于 {request['state']}，不能重新分析")
            metric = dict(conn.execute("SELECT * FROM metrics WHERE metric_id = ?", (request["metric_id"],)).fetchone())
            access = self._granted_access(conn, request_id, metric["source_dataset_id"])
            if access is None:
                raise PolicyError("没有有效的数据访问批准，不能重新分析")
            decision = self._latest_decision(conn, access["access_id"])
            parameters = self._analysis_parameters(
                conn, request, comparison=comparison, data_version=data_version, store_ids=store_ids, metric=metric
            )
            result = analysis.compute(
                conn, metric=metric, region_scope=request["region_scope"],
                stores=parameters["stores"], start=parameters["start"], end=parameters["end"],
                version=parameters["version"], comparison=comparison, purpose=request["purpose"],
            )
            if result["coverage"]["state"] == "no-data":
                raise ConflictError("选定数据版本在请求范围内没有数据，不能形成结论")
            result["access"] = {
                "access_id": access["access_id"],
                "output_scope": decision["output_scope"],
                "decision": decision["decision"],
                "policy_version": decision["policy_version"],
            }
            run_id = self.db.next_id(conn, "analysis", "RUN")
            now = self.db.now()
            conn.execute(
                "INSERT INTO analyses (run_id, request_id, metric_id, dataset_id, data_version, purpose, "
                "output_scope, parameters_json, metric_snapshot_json, result_json, coverage_json, lineage_json, "
                "content_hash, state, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                (
                    run_id, request_id, metric["metric_id"], metric["source_dataset_id"],
                    parameters["version"]["data_version"], request["purpose"], decision["output_scope"],
                    json.dumps({"comparison": comparison, "period_start": request["period_start"],
                                "period_end": request["period_end"],
                                "stores": [store["store_id"] for store in parameters["stores"]],
                                "data_version": parameters["version"]["data_version"]},
                               ensure_ascii=False, sort_keys=True),
                    json.dumps({**metric, "captured_at": now}, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    json.dumps(result["coverage"], ensure_ascii=False, sort_keys=True),
                    json.dumps({"dataset_id": metric["source_dataset_id"],
                                "stores": [store["store_id"] for store in parameters["stores"]],
                                "data_version": parameters["version"]["data_version"]},
                               ensure_ascii=False, sort_keys=True),
                    result["content_hash"], analyst_id, now,
                ),
            )
            conn.execute(
                "UPDATE requests SET state = 'in_analysis', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ?",
                (now, request_id),
            )
            self.db.audit(
                conn, actor_id=analyst_id, action="analysis.recreated", object_type="analysis",
                object_id=run_id, request_id=request_id,
                detail={"data_version": parameters["version"]["data_version"]},
            )
            return self._run_summary(conn, run_id, include_result=True)

    def publish_data_version(self, actor_id: str, data_version: str) -> dict:
        """发布新数据版本，并给受影响的已交付结论标注补齐范围。"""
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            self._require_role(actor, "data_governor")
            version = conn.execute("SELECT * FROM data_versions WHERE data_version = ?", (data_version,)).fetchone()
            if version is None:
                raise NotFoundError(f"未知数据版本：{data_version}")
            if version["status"] == "published":
                raise ConflictError(f"数据版本 {data_version} 已发布")
            now = self.db.now()
            conn.execute(
                "UPDATE data_versions SET status = 'published', published_at = ? WHERE data_version = ?",
                (now, data_version),
            )
            affected = []
            delivered = conn.execute(
                "SELECT * FROM analyses WHERE state = 'delivered' AND data_version IN "
                "(SELECT data_version FROM data_versions WHERE seq < ?) ORDER BY run_id",
                (version["seq"],),
            ).fetchall()
            for run in delivered:
                coverage = json.loads(run["coverage_json"])
                if coverage["state"] == "complete":
                    continue
                request = self._request(conn, run["request_id"])
                parameters = json.loads(run["parameters_json"])
                stores = parameters.get("stores", [])
                old_version = conn.execute(
                    "SELECT seq FROM data_versions WHERE data_version = ?", (run["data_version"],)
                ).fetchone()
                if old_version is None:
                    continue
                # 两个事实表共用批次标记，用门店日表判断补齐范围最完整。
                impact = analysis.coverage_impact(
                    conn, table="store_daily", stores=stores,
                    start=analysis.parse_date(parameters["period_start"]),
                    end=analysis.parse_date(parameters["period_end"]),
                    old_seq=int(old_version["seq"]), new_seq=int(version["seq"]),
                )
                if impact is None:
                    continue
                previous = conn.execute(
                    "SELECT impact_id FROM analysis_impacts WHERE run_id = ? AND kind = 'late-data-recovered' "
                    "AND source_ref = ?", (run["run_id"], data_version),
                ).fetchone()
                if previous:
                    continue
                impact_id = self.db.next_id(conn, "impact", "IM")
                conn.execute(
                    "INSERT INTO analysis_impacts (impact_id, run_id, kind, detected_at, source_ref, "
                    "data_version, previous_data_version, affected_start, affected_end, affected_stores, detail) "
                    "VALUES (?, ?, 'late-data-recovered', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        impact_id, run["run_id"], now, data_version, run["data_version"], run["data_version"],
                        impact["affected_start"], impact["affected_end"],
                        ",".join(impact["affected_stores"]),
                        f"新版本补齐 {impact['rows']} 条门店日记录；旧结论保留原版本 {run['data_version']}。",
                    ),
                )
                affected.append({"run_id": run["run_id"], "request_id": run["request_id"], **impact})
            self.db.audit(
                conn, actor_id=actor_id, action="data_version.published", object_type="data_version",
                object_id=data_version, detail={"affected_runs": [item["run_id"] for item in affected]},
            )
            return {
                "data_version": data_version,
                "status": "published",
                "affected_delivered_runs": affected,
            }

    # ------------------------------------------------------------------ 业务复核

    def review_delivery(
        self,
        owner_id: str,
        request_id: str,
        *,
        run_id: str,
        answered: bool,
        follow_up: bool,
        note: str,
    ) -> dict:
        text = note.strip()
        if not text:
            raise ValidationError("业务复核必须写明判断依据")
        if follow_up and not answered:
            raise ValidationError("未回答经营问题不能直接发起后续行动")
        with self.db.tx() as conn:
            owner = self._staff(conn, owner_id)
            self._require_role(owner, "business_owner")
            request = self._request(conn, request_id)
            if request["decision_owner_id"] != owner_id:
                raise AuthorizationError("只有该请求指定的业务负责人可以复核")
            if request["state"] != "delivered":
                raise ConflictError(f"请求处于 {request['state']}，只有已交付的结论可以复核")
            run = self._run(conn, run_id)
            if run["request_id"] != request_id or run["state"] != "delivered":
                raise ConflictError("只能复核该请求已交付的分析")
            latest = conn.execute(
                "SELECT run_id FROM analyses WHERE request_id = ? AND state = 'delivered' "
                "ORDER BY delivered_at DESC, run_id DESC LIMIT 1", (request_id,)
            ).fetchone()
            if latest["run_id"] != run_id:
                raise ConflictError("只能复核最近一次交付的结论")
            existing = conn.execute("SELECT review_id FROM reviews WHERE run_id = ?", (run_id,)).fetchone()
            if existing:
                raise ConflictError("该结论已经复核过")
            review_id = self.db.next_id(conn, "review", "RV")
            now = self.db.now()
            conn.execute(
                "INSERT INTO reviews (review_id, request_id, run_id, owner_id, answered, follow_up, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, request_id, run_id, owner_id, int(answered), int(follow_up), text, now),
            )
            if not answered:
                new_state = "closed_unanswered"
            elif follow_up:
                new_state = "follow_up_pending"
            else:
                new_state = "accepted_no_action"
            conn.execute(
                "UPDATE requests SET state = ?, updated_at = ?, row_version = row_version + 1 WHERE request_id = ?",
                (new_state, now, request_id),
            )
            self.db.audit(
                conn, actor_id=owner_id, action="delivery.reviewed", object_type="review",
                object_id=review_id, request_id=request_id,
                detail={"answered": answered, "follow_up": follow_up, "state": new_state},
            )
            return {"review_id": review_id, "request_state": new_state}

    # ------------------------------------------------------------------ 行动

    def propose_action(
        self,
        actor_id: str,
        request_id: str,
        *,
        decision: str,
        rationale: str,
        run_id: str | None = None,
    ) -> dict:
        if decision not in ACTION_DECISIONS:
            raise ValidationError(
                f"决定必须是：{', '.join(sorted(ACTION_DECISIONS))}。"
                "价格、采购、用工调整不在本应用的登记范围内，需走各自审批流程。"
            )
        text = rationale.strip()
        if not text:
            raise ValidationError("行动建议必须说明理由")
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            self._require_role(actor, "business_owner")
            request = self._request(conn, request_id)
            if request["state"] != "follow_up_pending":
                raise ConflictError(
                    f"请求处于 {request['state']}；只有业务负责人复核并选择跟进后，才能登记行动建议"
                )
            if request["decision_owner_id"] != actor_id:
                raise AuthorizationError("只有该请求的业务负责人可以登记行动")
            if run_id is None:
                row = conn.execute(
                    "SELECT run_id FROM analyses WHERE request_id = ? AND state = 'delivered' "
                    "ORDER BY delivered_at DESC, run_id DESC LIMIT 1", (request_id,)
                ).fetchone()
                if row is None:
                    raise ConflictError("没有已交付的结论可供行动引用")
                run_id = row["run_id"]
            run = self._run(conn, run_id)
            if run["request_id"] != request_id or run["state"] != "delivered":
                raise ConflictError("行动必须引用该请求已交付的结论")
            open_action = conn.execute(
                "SELECT action_id FROM actions WHERE request_id = ? AND state IN ('proposed', 'accepted')",
                (request_id,),
            ).fetchone()
            if open_action:
                raise ConflictError(f"该请求已有未结束的行动 {open_action['action_id']}")
            action_id = self.db.next_id(conn, "action", "ACT")
            now = self.db.now()
            conn.execute(
                "INSERT INTO actions (action_id, request_id, run_id, proposed_by, decision, rationale, "
                "state, proposed_at) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)",
                (action_id, request_id, run_id, actor_id, decision, text, now),
            )
            self.db.audit(
                conn, actor_id=actor_id, action="action.proposed", object_type="action",
                object_id=action_id, request_id=request_id,
                detail={"decision": decision, "run_id": run_id, "no_responsibility_until_acceptance": True},
            )
            return {
                "action_id": action_id,
                "state": "proposed",
                "note": "建议尚未形成责任和期限；需业务负责人接受后才生效。",
            }

    def accept_action(
        self,
        owner_id: str,
        action_id: str,
        *,
        due_at: str,
        note: str = "",
    ) -> dict:
        due = analysis.parse_date(due_at)
        with self.db.tx() as conn:
            owner = self._staff(conn, owner_id)
            self._require_role(owner, "business_owner")
            row = conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"未知行动：{action_id}")
            action = dict(row)
            if action["state"] != "proposed":
                raise ConflictError(f"行动状态为 {action['state']}，只有 proposed 可以接受")
            request = self._request(conn, action["request_id"])
            if request["decision_owner_id"] != owner_id:
                raise AuthorizationError("只有该请求指定的业务负责人可以接受行动")
            now = self.db.now()
            if due.isoformat() <= now[:10]:
                raise ValidationError("截止日期必须晚于接受日期")
            conn.execute(
                "UPDATE actions SET state = 'accepted', business_owner = ?, accepted_at = ?, due_at = ? "
                "WHERE action_id = ?",
                (owner_id, now, due.isoformat(), action_id),
            )
            conn.execute(
                "UPDATE requests SET state = 'action_pending', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ?",
                (now, request["request_id"]),
            )
            self.db.audit(
                conn, actor_id=owner_id, action="action.accepted", object_type="action",
                object_id=action_id, request_id=request["request_id"],
                detail={"due_at": due.isoformat(), "note": note.strip()},
            )
            return {
                "action_id": action_id,
                "state": "accepted",
                "business_owner": owner_id,
                "accepted_at": now,
                "due_at": due.isoformat(),
            }

    def start_action(self, actor_id: str, action_id: str) -> dict:
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            row = conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"未知行动：{action_id}")
            action = dict(row)
            if action["state"] != "accepted":
                raise ConflictError(f"行动状态为 {action['state']}，不能标记进行中")
            self._require_role(actor, "business_owner")
            if action["business_owner"] != actor_id:
                raise AuthorizationError("只有该行动的责任人可以登记执行")
            request = self._request(conn, action["request_id"])
            now = self.db.now()
            conn.execute(
                "UPDATE requests SET state = 'action_in_progress', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ? AND state = 'action_pending'",
                (now, request["request_id"]),
            )
            self.db.audit(conn, actor_id=actor_id, action="action.started", object_type="action",
                          object_id=action_id, request_id=action["request_id"])
            return {"action_id": action_id, "state": "accepted", "request_state": "action_in_progress"}

    def complete_action(
        self,
        actor_id: str,
        action_id: str,
        *,
        result_ref: str,
        result_note: str,
    ) -> dict:
        ref = result_ref.strip()
        text = result_note.strip()
        if not ref or not text:
            raise ValidationError("完成行动必须提供结果引用和结果说明")
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            row = conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"未知行动：{action_id}")
            action = dict(row)
            if action["state"] != "accepted":
                raise ConflictError(f"行动状态为 {action['state']}，只有已接受的行动可以完成")
            self._require_role(actor, "business_owner")
            if action["business_owner"] != actor_id:
                raise AuthorizationError("只有该行动的责任人可以登记完成")
            now = self.db.now()
            conn.execute(
                "UPDATE actions SET state = 'completed', completed_at = ?, result_ref = ?, result_note = ? "
                "WHERE action_id = ?",
                (now, ref, text, action_id),
            )
            conn.execute(
                "UPDATE requests SET state = 'completed', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ?",
                (now, action["request_id"]),
            )
            self.db.audit(
                conn, actor_id=actor_id, action="action.completed", object_type="action",
                object_id=action_id, request_id=action["request_id"],
                detail={"result_ref": ref},
            )
            return {
                "action_id": action_id,
                "state": "completed",
                "result_ref": ref,
                "request_id": action["request_id"],
            }

    def cancel_action(self, actor_id: str, action_id: str, *, reason: str) -> dict:
        text = reason.strip()
        if not text:
            raise ValidationError("取消行动必须说明原因")
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            row = conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"未知行动：{action_id}")
            action = dict(row)
            if action["state"] not in {"proposed", "accepted"}:
                raise ConflictError(f"行动状态为 {action['state']}，不能取消")
            request = self._request(conn, action["request_id"])
            self._require_role(actor, "business_owner")
            if request["decision_owner_id"] != actor_id:
                raise AuthorizationError("只有该请求的业务负责人可以取消行动")
            now = self.db.now()
            conn.execute(
                "UPDATE actions SET state = 'cancelled', cancelled_at = ?, cancel_reason = ? WHERE action_id = ?",
                (now, text, action_id),
            )
            conn.execute(
                "UPDATE requests SET state = 'follow_up_pending', updated_at = ?, row_version = row_version + 1 "
                "WHERE request_id = ? AND state IN ('action_pending', 'action_in_progress')",
                (now, request["request_id"]),
            )
            self.db.audit(conn, actor_id=actor_id, action="action.cancelled", object_type="action",
                          object_id=action_id, request_id=request["request_id"], detail={"reason": text})
            return {"action_id": action_id, "state": "cancelled"}

    # ------------------------------------------------------------------ 查询

    def request_detail(self, conn: sqlite3.Connection, *, actor: dict, request_id: str,
                       include_events: bool = True) -> dict:
        request = self._request(conn, request_id)
        metric = None
        if request["metric_id"]:
            row = conn.execute("SELECT * FROM metrics WHERE metric_id = ?", (request["metric_id"],)).fetchone()
            metric = dict(row) if row else None
        requester = dict(conn.execute("SELECT * FROM staff WHERE user_id = ?", (request["requester_id"],)).fetchone())
        owner = dict(conn.execute("SELECT * FROM staff WHERE user_id = ?", (request["decision_owner_id"],)).fetchone())
        analyst = None
        if request["assigned_analyst_id"]:
            analyst_row = conn.execute(
                "SELECT * FROM staff WHERE user_id = ?", (request["assigned_analyst_id"],)
            ).fetchone()
            analyst = dict(analyst_row) if analyst_row else None
        clarifications = self.db.all(
            conn, "SELECT * FROM clarifications WHERE request_id = ? ORDER BY created_at, clarification_id",
            (request_id,),
        )
        accesses = self.db.all(
            conn,
            "SELECT a.*, d.decision, d.output_scope, d.policy_id, d.policy_version, d.justification AS decision_note, "
            "d.created_at AS decided_at, g.display_name AS governor_name "
            "FROM access_requests a LEFT JOIN access_decisions d ON d.access_id = a.access_id "
            "LEFT JOIN staff g ON g.user_id = d.governor_id "
            "WHERE a.request_id = ? ORDER BY a.requested_at, a.access_id",
            (request_id,),
        )
        runs = self.db.all(
            conn,
            "SELECT run_id, metric_id, dataset_id, data_version, state, created_at, delivered_at, coverage_json, "
            "content_hash, delivery_note FROM analyses WHERE request_id = ? ORDER BY created_at, run_id",
            (request_id,),
        )
        for run in runs:
            run["coverage"] = json.loads(run.pop("coverage_json"))
            run["impacts"] = self.db.all(
                conn, "SELECT * FROM analysis_impacts WHERE run_id = ? ORDER BY detected_at, impact_id",
                (run["run_id"],),
            )
        reviews = self.db.all(conn, "SELECT * FROM reviews WHERE request_id = ? ORDER BY created_at", (request_id,))
        actions = self.db.all(
            conn, "SELECT * FROM actions WHERE request_id = ? ORDER BY proposed_at, action_id", (request_id,)
        )
        events = []
        if include_events:
            events = self.db.all(
                conn,
                "SELECT * FROM business_events WHERE region = ? AND start_date <= ? AND end_date >= ? "
                "ORDER BY start_date, event_id",
                (request["region_scope"], request["period_end"], request["period_start"]),
            )
        audit = self.db.all(
            conn,
            "SELECT event_id, occurred_at, actor_id, action, object_type, object_id, detail_json "
            "FROM audit_events WHERE request_id = ? ORDER BY event_id",
            (request_id,),
        )
        for event in audit:
            event["detail"] = json.loads(event.pop("detail_json"))
        return {
            "request": request,
            "requester": requester,
            "decision_owner": owner,
            "assigned_analyst": analyst,
            "metric": metric,
            "clarifications": clarifications,
            "accesses": accesses,
            "runs": runs,
            "reviews": reviews,
            "actions": actions,
            "events": events,
            "audit": audit,
            "available_datasets": self._available_datasets(conn, metric, request, requester),
        }

    def _available_datasets(self, conn: sqlite3.Connection, metric: dict | None,
                            request: dict, requester: dict) -> list[dict]:
        if metric:
            rows = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (metric["source_dataset_id"],)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM datasets WHERE active = 1 ORDER BY dataset_id").fetchall()
        result = []
        for row in rows:
            dataset = dict(row)
            try:
                evaluation = self.evaluate_policy(
                    dataset_id=dataset["dataset_id"], user_id=requester["user_id"], purpose=request["purpose"]
                )
                dataset["policy_decision"] = evaluation["decision"]
                dataset["policy_id"] = evaluation["policy_id"]
            except PolicyError as exc:
                dataset["policy_decision"] = "unknown"
                dataset["policy_note"] = str(exc)
            result.append(dataset)
        return result

    def get_request(self, actor_id: str, request_id: str) -> dict:
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            self._visible_request(conn, actor, request_id)
            return self.request_detail(conn, actor=actor, request_id=request_id)

    def list_requests(self, actor_id: str, *, state: str | None = None,
                      include_closed: bool = True) -> list[dict]:
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            sql = (
                "SELECT r.*, "
                "(SELECT COUNT(*) FROM access_requests a WHERE a.request_id = r.request_id AND a.status = 'granted') AS granted_access, "
                "(SELECT COUNT(*) FROM analyses n WHERE n.request_id = r.request_id AND n.state = 'delivered') AS delivered_runs, "
                "(SELECT COUNT(*) FROM actions c WHERE c.request_id = r.request_id AND c.state IN ('proposed','accepted')) AS open_actions "
                "FROM requests r WHERE 1 = 1"
            )
            params: list = []
            if actor["role"] == "store_manager":
                sql += " AND r.requester_id = ?"
                params.append(actor["user_id"])
            elif actor["role"] == "business_owner":
                sql += " AND r.decision_owner_id = ?"
                params.append(actor["user_id"])
            if state:
                sql += " AND r.state = ?"
                params.append(state)
            if not include_closed:
                sql += " AND r.state NOT IN ('completed', 'cancelled', 'closed_unanswered')"
            sql += " ORDER BY r.created_at, r.request_id"
            return self.db.all(conn, sql, params)

    def _run_summary(self, conn: sqlite3.Connection, run_id: str, *, include_result: bool = False) -> dict:
        run = self._run(conn, run_id)
        result = json.loads(run.pop("result_json"))
        metric = json.loads(run.pop("metric_snapshot_json"))
        parameters = json.loads(run.pop("parameters_json"))
        summary = {
            "run_id": run["run_id"],
            "request_id": run["request_id"],
            "state": run["state"],
            "data_version": run["data_version"],
            "metric_id": run["metric_id"],
            "dataset_id": run["dataset_id"],
            "coverage": json.loads(run["coverage_json"]),
            "content_hash": run["content_hash"],
            "created_at": run["created_at"],
            "delivered_at": run["delivered_at"],
            "parameters": parameters,
            "metric": metric,
            "impacts": self.db.all(
                conn, "SELECT * FROM analysis_impacts WHERE run_id = ? ORDER BY detected_at, impact_id", (run_id,)
            ),
            "result_summary": {
                "value": result.get("value"),
                "baseline": result.get("baseline"),
                "change": result.get("change"),
                "events": result.get("events", []),
                "diagnostics": result.get("diagnostics", []),
                "limitations": result.get("limitations", []),
                "stores": result.get("stores", []),
                "access": result.get("access", {}),
                "data_version": result.get("data_version"),
            },
        }
        if include_result:
            summary["result"] = result
        return summary

    def get_run(self, actor_id: str, run_id: str) -> dict:
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            run = self._run(conn, run_id)
            request = self._request(conn, run["request_id"])
            if not self._can_view_request(actor, request):
                raise AuthorizationError("不能查看该分析")
            return self._run_summary(conn, run_id, include_result=True)

    def list_actions(self, actor_id: str, *, state: str | None = None) -> list[dict]:
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            sql = (
                "SELECT a.*, r.department, r.region_scope, r.question_text, n.content_hash, n.data_version "
                "FROM actions a JOIN requests r ON r.request_id = a.request_id "
                "JOIN analyses n ON n.run_id = a.run_id WHERE 1 = 1"
            )
            params: list = []
            if actor["role"] == "store_manager":
                sql += " AND r.requester_id = ?"
                params.append(actor["user_id"])
            elif actor["role"] == "business_owner":
                sql += " AND r.decision_owner_id = ?"
                params.append(actor["user_id"])
            if state:
                sql += " AND a.state = ?"
                params.append(state)
            sql += " ORDER BY a.proposed_at, a.action_id"
            return self.db.all(conn, sql, params)

    def audit_trail(self, actor_id: str, request_id: str) -> list[dict]:
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            self._visible_request(conn, actor, request_id)
            return self.db.all(
                conn,
                "SELECT event_id, occurred_at, actor_id, action, object_type, object_id, detail_json "
                "FROM audit_events WHERE request_id = ? ORDER BY event_id",
                (request_id,),
            )

    def workflow_dashboard(self, actor_id: str) -> dict:
        with self.db.tx() as conn:
            actor = self._staff(conn, actor_id)
            rows = self.list_requests(actor_id)
            states: dict[str, int] = {}
            for row in rows:
                states[row["state"]] = states.get(row["state"], 0) + 1
            return {
                "actor": actor,
                "counts_by_state": states,
                "total_visible_requests": len(rows),
                "open_actions": len(self.list_actions(actor_id, state="accepted")),
                "proposed_actions": len(self.list_actions(actor_id, state="proposed")),
                "roles": {
                    "store_manager": "登记问题、补充信息、查看自己的请求",
                    "analyst": "澄清问题、选择口径、生成并交付可复核结论",
                    "data_governor": "决定数据访问、撤销授权、发布数据版本",
                    "business_owner": "复核结论、接受行动、登记完成结果",
                },
            }
