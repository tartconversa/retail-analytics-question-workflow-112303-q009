"""合成资料与端到端工作流测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from retail_analytics_question_workflow.db import Database
from retail_analytics_question_workflow.errors import (
    AuthorizationError,
    ConflictError,
    PolicyError,
    ValidationError,
)
from retail_analytics_question_workflow.materials import connect, initialize
from retail_analytics_question_workflow.service import WorkflowService

MATERIALS = ROOT / "data" / "workflow"


class WorkflowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "workflow.db"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        initialize(self.conn, MATERIALS, reset=True)
        self.service = WorkflowService(Database(self.conn))

    # ------------------------------------------------------------------ 辅助

    def register(self, *, requester="U-101", region="R-N", start="2026-11-03",
                 end="2026-11-16", purpose="diagnosis", owner="MGR-101",
                 question="北区近两周到店转化为什么下降？"):
        return self.service.register_request(
            requester, question_text=question, region_scope=region,
            period_start=start, period_end=end, purpose=purpose,
            decision_owner_id=owner,
        )["request"]["request_id"]

    def to_ready(self, request_id: str, *, metric="M-CONV-OPS",
                 dataset="DS-STORE-DAILY", decision="allow",
                 output_scope="store-aggregate", analyst="AN-101",
                 requester="U-101") -> str:
        self.service.add_clarification(analyst, request_id, kind="question", message="确认口径")
        self.service.add_clarification(requester, request_id, kind="answer", message="按门店汇总")
        self.service.set_metric(analyst, request_id, metric_id=metric,
                                clarified_question="按选定口径比较前后两期")
        access = self.service.request_access(
            analyst, request_id, dataset_id=dataset, justification="经营诊断所需"
        )
        self.service.decide_access(
            "GOV-101", access["access_id"], decision=decision,
            justification="范围与用途匹配", output_scope=output_scope,
        )
        return access["access_id"]

    def deliver(self, request_id: str, *, metric="M-CONV-OPS", dataset="DS-STORE-DAILY",
                decision="allow", end="2026-11-16", purpose="diagnosis",
                note="覆盖不完整，结论只含已到数据。", analyst="AN-101",
                requester="U-101"):
        self.service.add_clarification(analyst, request_id, kind="question", message="确认口径")
        self.service.add_clarification(requester, request_id, kind="answer", message="按门店汇总")
        self.service.set_metric(analyst, request_id, metric_id=metric,
                                clarified_question="按选定口径比较前后两期")
        access = self.service.request_access(analyst, request_id, dataset_id=dataset,
                                             justification="经营诊断所需")
        self.service.decide_access("GOV-101", access["access_id"], decision=decision,
                                   justification="范围与用途匹配", output_scope="store-aggregate")
        run = self.service.run_analysis(analyst, request_id)
        return self.service.deliver_analysis(
            analyst, run["run_id"], note=note, acknowledge_partial=True
        )


class MaterialTests(WorkflowTestCase):
    def test_materials_load_with_expected_counts(self):
        counts = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "staff", "stores", "datasets", "metrics", "access_policies",
                "business_events", "data_versions", "store_daily", "member_daily",
            )
        }
        self.assertEqual(counts["staff"], 16)
        self.assertEqual(counts["stores"], 12)
        self.assertEqual(counts["metrics"], 10)
        self.assertEqual(counts["store_daily"], 12 * 115)
        self.assertGreater(counts["member_daily"], 1000)

    def test_problem_sales_definitions_are_department_specific(self):
        rows = self.service.db.all(
            self.conn, "SELECT * FROM metrics WHERE metric_name = 'net-sales' AND status = 'active'"
        )
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual({row["definition_version"] for row in rows}, {"DEF-1", "DEF-2"})
        self.assertEqual(len({row["calculation_key"] for row in rows}), 2)


class RequestLifecycleTests(WorkflowTestCase):
    def test_registration_records_scope_and_owner(self):
        request_id = self.register()
        detail = self.service.get_request("U-101", request_id)
        self.assertEqual(detail["request"]["state"], "new")
        self.assertEqual(detail["request"]["decision_owner_id"], "MGR-101")
        self.assertEqual(detail["request"]["region_scope"], "R-N")
        self.assertEqual(detail["events"][0]["event_id"], "EV-N-ROADWORKS")

    def test_only_store_manager_can_register(self):
        with self.assertRaises(AuthorizationError):
            self.register(requester="AN-101")

    def test_requester_cannot_register_outside_own_region(self):
        with self.assertRaises(AuthorizationError):
            self.register(requester="U-102", region="R-N")

    def test_metric_outside_audience_is_rejected(self):
        request_id = self.register(requester="U-102", region="R-S", owner="MGR-102")
        self.service.add_clarification("AN-101", request_id, kind="question", message="确认口径")
        with self.assertRaises(PolicyError) as context:
            self.service.set_metric("AN-101", request_id, metric_id="M-SALES-FIN",
                                    clarified_question="比较销售额")
        self.assertIn("适用人群", str(context.exception))

    def test_review_metric_cannot_be_delivered(self):
        request_id = self.register()
        with self.assertRaises(PolicyError):
            self.service.set_metric("AN-101", request_id, metric_id="M-CONV-REVIEW",
                                    clarified_question="比较转化率")

    def test_unauthorized_actor_cannot_see_request(self):
        request_id = self.register()
        with self.assertRaises(AuthorizationError):
            self.service.get_request("U-104", request_id)

    def test_analyst_cannot_start_before_access_granted(self):
        request_id = self.register()
        self.service.add_clarification("AN-101", request_id, kind="question", message="确认口径")
        self.service.set_metric("AN-101", request_id, metric_id="M-CONV-OPS",
                                clarified_question="比较转化率")
        with self.assertRaises(ConflictError):
            self.service.run_analysis("AN-101", request_id)


class AccessAndPolicyTests(WorkflowTestCase):
    def test_policy_denies_member_data_for_diagnosis(self):
        evaluation = self.service.evaluate_policy(
            dataset_id="DS-MEMBER-DAILY", user_id="U-101", purpose="diagnosis"
        )
        self.assertEqual(evaluation["decision"], "deny")
        self.assertEqual(evaluation["policy_id"], "POL-MD-DIAG")

    def test_member_access_requires_stricter_scope_and_is_capped(self):
        request_id = self.register(purpose="member-followup")
        self.service.add_clarification("AN-101", request_id, kind="question", message="确认口径")
        self.service.set_metric("AN-101", request_id, metric_id="M-MEMBER-OPS",
                                clarified_question="会员到店笔数")
        access = self.service.request_access(
            "AN-101", request_id, dataset_id="DS-MEMBER-DAILY", justification="会员跟进"
        )
        with self.assertRaises(PolicyError):
            self.service.decide_access("GOV-101", access["access_id"], decision="allow",
                                       justification="放宽", output_scope="store-aggregate")
        with self.assertRaises(PolicyError):
            self.service.decide_access("GOV-101", access["access_id"], decision="limit",
                                       justification="明细", output_scope="row-detail")
        result = self.service.decide_access("GOV-101", access["access_id"], decision="limit",
                                            justification="门店聚合", output_scope="store-aggregate")
        self.assertEqual(result["status"], "granted")

    def test_member_requester_without_clearance_is_rejected(self):
        request_id = self.register(requester="U-104", region="R-S", owner="MGR-104",
                                   purpose="member-followup")
        self.service.add_clarification("AN-101", request_id, kind="question", message="确认口径")
        self.service.set_metric("AN-101", request_id, metric_id="M-MEMBER-OPS",
                                clarified_question="会员到店笔数")
        with self.assertRaises(PolicyError) as context:
            self.service.request_access("AN-101", request_id, dataset_id="DS-MEMBER-DAILY",
                                        justification="会员跟进")
        self.assertIn("个人数据", str(context.exception))

    def test_governor_can_deny_even_without_personal_clearance(self):
        request_id = self.register(requester="U-104", region="R-S", owner="MGR-104",
                                   purpose="member-followup")
        self.service.add_clarification("AN-101", request_id, kind="question", message="确认口径")
        self.service.set_metric("AN-101", request_id, metric_id="M-MEMBER-OPS",
                                clarified_question="会员到店笔数")
        # 申请阶段策略即拒绝，正常流程无法进入待审；这里构造一条历史待审申请，
        # 验证治理人员仍可作出 deny，而不会因为提问者没有许可而卡住。
        with self.service.db.tx() as conn:
            conn.execute(
                "UPDATE requests SET state = 'access_pending' WHERE request_id = ?", (request_id,)
            )
            conn.execute(
                "INSERT INTO access_requests (access_id, request_id, dataset_id, purpose, requested_by, "
                "requested_at, justification, status) VALUES ('AC-TEST', ?, 'DS-MEMBER-DAILY', "
                "'member-followup', 'AN-101', '2026-11-24T00:00:00Z', '测试', 'pending')",
                (request_id,),
            )
        result = self.service.decide_access(
            "GOV-101", "AC-TEST", decision="deny", justification="提问者无个人数据许可",
            output_scope="store-aggregate",
        )
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.service.get_request("MGR-104", request_id)["request"]["state"], "blocked")

    def test_access_cannot_be_requested_for_unrelated_dataset(self):
        request_id = self.register()
        self.service.add_clarification("AN-101", request_id, kind="question", message="确认口径")
        self.service.set_metric("AN-101", request_id, metric_id="M-CONV-OPS",
                                clarified_question="比较转化率")
        with self.assertRaises(PolicyError):
            self.service.request_access("AN-101", request_id, dataset_id="DS-MEMBER-DAILY",
                                        justification="不相干数据")

    def test_duplicate_pending_access_is_rejected(self):
        request_id = self.register()
        self.service.add_clarification("AN-101", request_id, kind="question", message="确认口径")
        self.service.set_metric("AN-101", request_id, metric_id="M-CONV-OPS",
                                clarified_question="比较转化率")
        self.service.request_access("AN-101", request_id, dataset_id="DS-STORE-DAILY",
                                    justification="诊断")
        with self.assertRaises(ConflictError):
            self.service.request_access("AN-101", request_id, dataset_id="DS-STORE-DAILY",
                                        justification="重复")


class AnalysisTests(WorkflowTestCase):
    def test_analysis_is_deterministic_and_traceable(self):
        request_id = self.register()
        self.to_ready(request_id)
        first = self.service.run_analysis("AN-101", request_id)
        second = self.service.run_analysis("AN-101", request_id)
        self.assertEqual(first["content_hash"], second["content_hash"])
        result = first["result"]
        self.assertEqual(result["data_version"], "DV-2026-11-17")
        self.assertEqual(result["metric"]["definition_version"], "DEF-1")
        self.assertEqual(result["coverage"]["state"], "late-data")
        self.assertIn("N-301", result["scope"]["stores"])
        self.assertGreater(result["row_count"], 0)

    def test_late_data_is_not_treated_as_zero(self):
        request_id = self.register()
        self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id)
        coverage = run["coverage"]
        self.assertEqual(coverage["state"], "late-data")
        self.assertGreater(coverage["missing_rows"], 0)
        self.assertEqual(coverage["covered_rows"] + coverage["missing_rows"],
                         coverage["expected_rows"])
        missing_stores = {store for item in coverage["missing_ranges"] for store in item["stores"]}
        self.assertEqual(missing_stores, {"N-301", "N-303"})

    def test_partial_delivery_requires_acknowledgement_and_note(self):
        request_id = self.register()
        self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id)
        with self.assertRaises(ConflictError):
            self.service.deliver_analysis("AN-101", run["run_id"])
        with self.assertRaises(ValidationError):
            self.service.deliver_analysis("AN-101", run["run_id"], acknowledge_partial=True)
        delivered = self.service.deliver_analysis(
            "AN-101", run["run_id"],
            note="11-10 至 11-16 北苑店与科技园店数据迟到，结论只覆盖已到数据。",
            acknowledge_partial=True,
        )
        self.assertEqual(delivered["state"], "delivered")
        self.assertEqual(self.service.get_request("U-101", request_id)["request"]["state"], "delivered")

    def test_outside_coverage_is_flagged(self):
        request_id = self.register(start="2026-11-10", end="2026-11-23")
        self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id)
        self.assertEqual(run["coverage"]["state"], "outside-coverage")
        self.assertIn("超出", run["coverage"]["note"])

    def test_complete_coverage_delivers_without_partial_acknowledgement(self):
        self.service.publish_data_version("GOV-101", "DV-2026-11-24")
        request_id = self.register()
        self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id, data_version="DV-2026-11-24")
        self.assertEqual(run["coverage"]["state"], "complete")
        delivered = self.service.deliver_analysis("AN-101", run["run_id"])
        self.assertEqual(delivered["state"], "delivered")

    def test_period_entirely_after_coverage_is_rejected_as_no_data(self):
        request_id = self.register(start="2026-11-17", end="2026-11-23")
        self.to_ready(request_id)
        with self.assertRaises(ConflictError) as context:
            self.service.run_analysis("AN-101", request_id, data_version="DV-2026-11-10")
        self.assertIn("没有数据", str(context.exception))

    def test_reanalysis_after_revocation_is_rejected(self):
        request_id = self.register()
        access_id = self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id)
        self.service.deliver_analysis("AN-101", run["run_id"], note="迟到数据。",
                                      acknowledge_partial=True)
        self.service.revoke_access("GOV-101", access_id, reason="复核授权")
        with self.assertRaises(PolicyError):
            self.service.reanalyze("AN-101", request_id, data_version="DV-2026-11-24")

    def test_out_of_scope_store_is_rejected(self):
        request_id = self.register()
        self.to_ready(request_id)
        with self.assertRaises(ValidationError):
            self.service.run_analysis("AN-101", request_id, store_ids=["S-101"])

    def test_events_are_attached_to_conclusion(self):
        request_id = self.register(start="2026-11-01", end="2026-11-16")
        self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id)
        event_ids = {event["event_id"] for event in run["result"]["events"]}
        self.assertIn("EV-N-ROADWORKS", event_ids)

    def test_net_sales_definition_changes_the_number(self):
        from datetime import date

        from retail_analytics_question_workflow import analysis

        stores = analysis.resolve_stores(self.conn, "R-N", None)
        version = analysis.find_version(self.conn, "DV-2026-11-17")
        results = {}
        for metric_id in ("M-SALES-FIN", "M-SALES-MERCH"):
            metric = dict(self.conn.execute(
                "SELECT * FROM metrics WHERE metric_id = ?", (metric_id,)
            ).fetchone())
            results[metric_id] = analysis.compute(
                self.conn, metric=metric, region_scope="R-N", stores=stores,
                start=date(2026, 11, 1), end=date(2026, 11, 9), version=version,
            )
        finance = results["M-SALES-FIN"]
        merch = results["M-SALES-MERCH"]
        self.assertNotEqual(finance["content_hash"], merch["content_hash"])
        self.assertEqual(finance["metric"]["definition_version"], "DEF-1")
        self.assertEqual(merch["metric"]["definition_version"], "DEF-2")
        self.assertGreater(merch["value"]["value"], finance["value"]["value"])

    def test_member_metric_only_returns_suppressed_store_aggregates(self):
        request_id = self.register(purpose="member-followup", start="2026-11-03", end="2026-11-09")
        self.to_ready(request_id, metric="M-MEMBER-OPS", dataset="DS-MEMBER-DAILY", decision="limit")
        run = self.service.run_analysis("AN-101", request_id)
        result = run["result"]
        self.assertNotIn("member_ref", json.dumps(result["value"]))
        self.assertTrue(all("member_ref" not in store for store in result["stores"]))
        self.assertEqual(result["access"]["output_scope"], "store-aggregate")


class ImpactTests(WorkflowTestCase):
    def test_publishing_new_version_marks_recovered_range(self):
        request_id = self.register()
        self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id)
        self.service.deliver_analysis(
            "AN-101", run["run_id"], note="迟到数据，只覆盖已到部分。", acknowledge_partial=True
        )
        published = self.service.publish_data_version("GOV-101", "DV-2026-11-24")
        self.assertEqual(len(published["affected_delivered_runs"]), 1)
        detail = self.service.get_request("AN-101", request_id)
        impacts = detail["runs"][0]["impacts"]
        self.assertEqual(impacts[0]["kind"], "late-data-recovered")
        self.assertEqual(impacts[0]["affected_start"], "2026-11-10")
        self.assertEqual(impacts[0]["affected_end"], "2026-11-16")
        self.assertEqual(impacts[0]["affected_stores"], "N-301,N-303")

    def test_reanalysis_at_new_version_preserves_old_run(self):
        request_id = self.register()
        self.to_ready(request_id)
        old = self.service.run_analysis("AN-101", request_id)
        self.service.deliver_analysis(
            "AN-101", old["run_id"], note="迟到数据。", acknowledge_partial=True
        )
        self.service.publish_data_version("GOV-101", "DV-2026-11-24")
        new = self.service.reanalyze("AN-101", request_id, data_version="DV-2026-11-24")
        self.assertEqual(new["coverage"]["state"], "complete")
        self.assertNotEqual(new["content_hash"], old["content_hash"])
        runs = self.service.get_request("AN-101", request_id)["runs"]
        self.assertEqual(len(runs), 2)
        self.assertEqual({run["data_version"] for run in runs},
                         {"DV-2026-11-17", "DV-2026-11-24"})

    def test_revoke_marks_delivered_run_but_keeps_content(self):
        request_id = self.register(purpose="member-followup")
        access_id = self.to_ready(request_id, metric="M-MEMBER-OPS",
                                  dataset="DS-MEMBER-DAILY", decision="limit")
        run = self.service.run_analysis("AN-101", request_id)
        delivered = self.service.deliver_analysis(
            "AN-101", run["run_id"], note="会员到店聚合。", acknowledge_partial=True
        ) if run["coverage"]["state"] != "complete" else self.service.deliver_analysis(
            "AN-101", run["run_id"]
        )
        revoked = self.service.revoke_access("GOV-101", access_id, reason="授权范围需要复核")
        self.assertEqual(revoked["affected_runs"], [delivered["run_id"]])
        detail = self.service.get_request("AN-101", request_id)
        self.assertEqual(detail["runs"][0]["content_hash"], run["content_hash"])
        self.assertEqual(detail["runs"][0]["impacts"][0]["kind"], "permission-revoked")
        self.assertEqual(detail["request"]["state"], "delivered")


class ReviewAndActionTests(WorkflowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.request_id = self.register()
        self.run = self.deliver(self.request_id)

    def test_only_decision_owner_can_review(self):
        with self.assertRaises(AuthorizationError):
            self.service.review_delivery("MGR-102", self.request_id, run_id=self.run["run_id"],
                                         answered=True, follow_up=True, note="越权")

    def test_action_proposal_requires_business_review(self):
        with self.assertRaises(ConflictError):
            self.service.propose_action("MGR-101", self.request_id, decision="investigate",
                                        rationale="先观察")

    def test_action_does_not_create_obligation_until_accepted(self):
        self.service.review_delivery("MGR-101", self.request_id, run_id=self.run["run_id"],
                                     answered=True, follow_up=True, note="与施工时间重叠")
        proposed = self.service.propose_action("MGR-101", self.request_id, decision="investigate",
                                               rationale="观察施工结束后转化恢复")
        self.assertEqual(proposed["state"], "proposed")
        actions = self.service.list_actions("MGR-101", state="proposed")
        self.assertEqual(actions[0]["due_at"], "")
        accepted = self.service.accept_action("MGR-101", proposed["action_id"], due_at="2026-12-20")
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(self.service.get_request("MGR-101", self.request_id)["request"]["state"],
                         "action_pending")

    def test_other_owner_cannot_accept_action(self):
        self.service.review_delivery("MGR-101", self.request_id, run_id=self.run["run_id"],
                                     answered=True, follow_up=True, note="需要跟进")
        proposed = self.service.propose_action("MGR-101", self.request_id, decision="observe",
                                               rationale="观察")
        with self.assertRaises(AuthorizationError):
            self.service.accept_action("MGR-102", proposed["action_id"], due_at="2026-12-20")

    def test_completion_returns_result_to_request(self):
        self.service.review_delivery("MGR-101", self.request_id, run_id=self.run["run_id"],
                                     answered=True, follow_up=True, note="需要跟进")
        proposed = self.service.propose_action("MGR-101", self.request_id, decision="change-display",
                                               rationale="调整陈列")
        self.service.accept_action("MGR-101", proposed["action_id"], due_at="2026-12-20")
        with self.assertRaises(ValidationError):
            self.service.complete_action("MGR-101", proposed["action_id"], result_ref="", result_note="")
        done = self.service.complete_action("MGR-101", proposed["action_id"],
                                            result_ref="RES-01", result_note="已调整并观察两周")
        self.assertEqual(done["state"], "completed")
        detail = self.service.get_request("MGR-101", self.request_id)
        self.assertEqual(detail["request"]["state"], "completed")
        self.assertEqual(detail["actions"][0]["result_ref"], "RES-01")

    def test_price_change_is_not_recordable_as_action(self):
        with self.assertRaises(ValidationError) as context:
            self.service.propose_action("MGR-101", self.request_id, decision="change-price",
                                        rationale="提价")
        self.assertIn("价格", str(context.exception))

    def test_cancelled_action_returns_to_follow_up_pending(self):
        self.service.review_delivery("MGR-101", self.request_id, run_id=self.run["run_id"],
                                     answered=True, follow_up=True, note="需要跟进")
        proposed = self.service.propose_action("MGR-101", self.request_id, decision="observe",
                                               rationale="观察")
        self.service.accept_action("MGR-101", proposed["action_id"], due_at="2026-12-20")
        self.service.cancel_action("MGR-101", proposed["action_id"], reason="门店已关闭")
        detail = self.service.get_request("MGR-101", self.request_id)
        self.assertEqual(detail["request"]["state"], "follow_up_pending")
        self.assertEqual(detail["actions"][0]["cancel_reason"], "门店已关闭")

    def test_unanswered_review_closes_without_action(self):
        result = self.service.review_delivery("MGR-101", self.request_id, run_id=self.run["run_id"],
                                              answered=False, follow_up=False, note="口径不覆盖问题")
        self.assertEqual(result["request_state"], "closed_unanswered")
        with self.assertRaises(ConflictError):
            self.service.review_delivery("MGR-101", self.request_id, run_id=self.run["run_id"],
                                         answered=True, follow_up=True, note="重复复核")


class AuditTests(WorkflowTestCase):
    def test_audit_trail_is_append_only_and_ordered(self):
        request_id = self.register()
        self.to_ready(request_id)
        run = self.service.run_analysis("AN-101", request_id)
        self.service.deliver_analysis("AN-101", run["run_id"], note="迟到数据。",
                                      acknowledge_partial=True)
        trail = self.service.audit_trail("AN-101", request_id)
        actions = [event["action"] for event in trail]
        self.assertEqual(actions[0], "request.registered")
        self.assertIn("request.metric_selected", actions)
        self.assertIn("access.requested", actions)
        self.assertIn("access.decided", actions)
        self.assertIn("analysis.created", actions)
        self.assertIn("analysis.delivered", actions)
        self.assertEqual([event["event_id"] for event in trail],
                         sorted(event["event_id"] for event in trail))

    def test_only_visible_actors_can_read_audit(self):
        request_id = self.register()
        with self.assertRaises(AuthorizationError):
            self.service.audit_trail("U-105", request_id)

    def test_dashboard_counts_only_visible_work(self):
        request_id = self.register()
        dashboard = self.service.workflow_dashboard("U-101")
        self.assertEqual(dashboard["total_visible_requests"], 1)
        self.assertEqual(dashboard["counts_by_state"], {"new": 1})
        analyst = self.service.workflow_dashboard("AN-101")
        self.assertEqual(analyst["total_visible_requests"], 1)
        self.assertEqual(self.service.workflow_dashboard("U-102")["total_visible_requests"], 0)


if __name__ == "__main__":
    unittest.main()
