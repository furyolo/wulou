from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.main import CurationServer, ServiceState


class HttpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.tempdir.name)
        settings_path = self.temp_path / "settings.yaml"
        settings_path.write_text(yaml.safe_dump({
            "host": "127.0.0.1",
            "port": 0,
            "taxonomy_path": str(ROOT / "config" / "taxonomy.example.yaml"),
            "rules_path": str(ROOT / "config" / "classification-rules.yaml"),
            "cache_path": "cache.sqlite3",
        }, allow_unicode=True), encoding="utf-8")
        self.state = ServiceState(settings_path)
        self.server = CurationServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.state.close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Content-Type": "application/json"}
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        connection.request(method, path, body=raw_body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_health_is_available_without_token(self) -> None:
        status, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["port"], self.server.server_port)

    def test_taxonomy_is_available_on_loopback_without_token(self) -> None:
        status, payload = self.request("GET", "/api/v1/taxonomy")
        self.assertEqual(status, 200)
        self.assertIn("topics", payload)

    def test_classification_is_cached(self) -> None:
        body = {
            "exercise_id": "2529221",
            "question_press": "计算并化简含有分母有理化的根式",
            "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
        }
        first_status, first = self.request("POST", "/api/v1/classify", body)
        second_status, second = self.request("POST", "/api/v1/classify", body)
        self.assertEqual(first_status, 200)
        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["model_input_snapshot"]["question"]["text"], body["question_press"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["target"]["level4_id"], "real-number-calculation-rationalization")

        deleted_status, deleted = self.request("POST", "/api/v1/cache/classifications/delete", {
            "exercise_ids": [body["exercise_id"]],
        })
        self.assertEqual(deleted_status, 200)
        self.assertEqual(deleted["deleted"], 1)
        refreshed_status, refreshed = self.request("POST", "/api/v1/classify", body)
        self.assertEqual(refreshed_status, 200)
        self.assertFalse(refreshed["cache_hit"])

    def test_cached_classifications_can_be_recovered_after_a_lost_browser_callback(self) -> None:
        body = {
            "exercise_id": "cache-recovery-1",
            "question_press": "计算并化简含有分母有理化的根式",
            "current_catalogue_id": "level4-a",
            "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
        }
        classified_status, _ = self.request("POST", "/api/v1/classify", body)
        lookup_status, lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [body]}
        )
        self.assertEqual(classified_status, 200)
        self.assertEqual(lookup_status, 200)
        self.assertEqual(lookup["missing_exercise_ids"], [])
        self.assertEqual(lookup["results"][0]["exercise_id"], body["exercise_id"])
        self.assertTrue(lookup["results"][0]["cache_hit"])

    def test_cache_recovery_ignores_dynamic_card_text(self) -> None:
        body = {
            "exercise_id": "cache-dynamic-source-1",
            "question_press": "计算并化简含有分母有理化的根式",
            "answer_press": "答案文本",
            "question_image_url": "https://example.test/question.png",
            "current_catalogue_id": "level4-a",
            "source": "已贴知识点分组：旧页面状态",
            "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
        }
        self.request("POST", "/api/v1/classify", body)
        refreshed = {
            **body,
            "source": "已贴知识点分组：刷新后的页面状态",
            "scope": {"topic_id": "topic-99", "level2_id": "large-99"},
        }
        status, lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [refreshed]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(lookup["missing_exercise_ids"], [])
        self.assertTrue(lookup["results"][0]["cache_hit"])

    def test_manual_classification_overrides_model_cache_in_the_same_directory_context(self) -> None:
        body = {
            "exercise_id": "manual-override-1",
            "stable_code": "CS2026MANUAL001",
            "current_catalogue_id": "level4-a",
        }
        saved_status, saved = self.request("POST", "/api/v1/manual-classifications", {
            **body,
            "original_target_path": ["专题1：实数", "【大题】", "旧分类"],
            "target_path": ["专题1：实数", "【大题】", "分母有理化"],
        })
        self.assertEqual(saved_status, 201)
        self.assertEqual(saved["source_catalogue_id"], "level4-a")

        lookup_status, lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [body]}
        )
        self.assertEqual(lookup_status, 200)
        self.assertEqual(lookup["missing_exercise_ids"], [])
        result = lookup["results"][0]
        self.assertEqual(result["target"]["path"][-1], "分母有理化")
        self.assertEqual(result["manual_override"]["source"], "manual")

    def test_catalogue_move_history_reports_latest_confirmed_move(self) -> None:
        first = {
            "exercise_id": "move-history-1", "stable_code": "CS2026MOVE001",
            "source_catalogue_id": "old-leaf", "target_catalogue_id": "middle-leaf",
            "original_path": ["专题1：实数", "【大题】", "旧分类"],
            "target_path": ["专题4：分式方程与不等式", "【大题】", "解不等式"],
        }
        second = {
            **first,
            "source_catalogue_id": "middle-leaf", "target_catalogue_id": "new-leaf",
            "original_path": first["target_path"],
            "target_path": ["专题10：三角形", "【大题】", "全等三角形", "考法2"],
        }
        self.assertEqual(self.request("POST", "/api/v1/history/catalogue-moves", first)[0], 201)
        self.assertEqual(self.request("POST", "/api/v1/history/catalogue-moves", second)[0], 201)
        status, report = self.request("GET", "/api/v1/history/catalogue-moves")
        self.assertEqual(status, 200)
        self.assertEqual(report["summary"]["classified_count"], 1)
        self.assertEqual(report["summary"]["topics"], ["专题10：三角形"])
        self.assertEqual(report["records"][0]["stable_code"], "CS2026MOVE001")
        self.assertEqual(report["records"][0]["original_path"], first["target_path"])
        self.assertEqual(report["records"][0]["target_path"], second["target_path"])

    def test_catalogue_move_history_allows_unknown_original_path_but_not_missing_target(self) -> None:
        body = {
            "exercise_id": "move-history-unknown-source", "stable_code": "CS2026MOVE003",
            "source_catalogue_id": "outside-page-tree", "target_catalogue_id": "level3-leaf",
            "original_path": [],
            "target_path": ["专题4：分式方程与不等式", "【大题】", "解不等式"],
        }
        status, saved = self.request("POST", "/api/v1/history/catalogue-moves", body)
        self.assertEqual(status, 201)
        self.assertEqual(saved["exercise_id"], body["exercise_id"])

    def test_cloud_settings_save_key_without_returning_it(self) -> None:
        status, payload = self.request("POST", "/api/v1/settings/cloud", {
            "model": "test-cloud-model", "routing_model": "test-routing-model",
            "reasoning_effort": "high", "routing_reasoning_effort": "medium",
            "audit_mode": "conditional", "max_concurrent_requests": 5,
            "base_url": "https://api.example.test/v1", "api_key": "test-secret-key-123",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "test-cloud-model")
        self.assertEqual(payload["routing_model"], "test-routing-model")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["routing_reasoning_effort"], "medium")
        self.assertEqual(payload["audit_mode"], "conditional")
        self.assertEqual(payload["max_concurrent_requests"], 5)
        self.assertTrue(payload["api_key_configured"])
        self.assertNotIn("api_key", payload)
        persisted = yaml.safe_load((self.temp_path / "settings.yaml").read_text(encoding="utf-8"))
        self.assertEqual(persisted["classifier"]["cloud"]["api_key"], "test-secret-key-123")
        self.assertEqual(persisted["classifier"]["cloud"]["routing_model"], "test-routing-model")
        self.assertEqual(persisted["classifier"]["cloud"]["routing_reasoning_effort"], "medium")
        self.assertEqual(persisted["classifier"]["cloud"]["audit_mode"], "conditional")
        self.assertEqual(persisted["classifier"]["cloud"]["max_concurrent_requests"], 5)

    def test_conditional_audit_only_skips_low_risk_topic_after_self_check(self) -> None:
        self.state.settings["classifier"] = {"cloud": {"audit_mode": "conditional"}}
        question = {"question_press": "已知函数关系，求对应值"}
        decision = {"self_check": {"passed": True, "violations": [], "reason": "一致", "confidence": 0.98}}
        low_risk_target = SimpleNamespace(topic_order=11, level2_title="【微专题】")
        self.assertFalse(self.state._requires_independent_audit(question, decision, low_risk_target))
        self.assertTrue(self.state._requires_independent_audit(
            {**question, "question_latex": r"\frac{1{2}"}, decision, low_risk_target
        ))
        self.assertTrue(self.state._requires_independent_audit(
            question, decision, SimpleNamespace(topic_order=10, level2_title="【大题】")
        ))

    def test_disabled_audit_never_schedules_a_third_model_request(self) -> None:
        self.state.settings["classifier"] = {"cloud": {"audit_mode": "disabled"}}
        question = {"question_press": "计算并化简"}
        decision = {"self_check": {"passed": False, "violations": ["需复核"]}}
        high_risk_target = SimpleNamespace(topic_order=1, level2_title="【大题】")
        self.assertEqual(self.state.audit_mode(), "disabled")
        self.assertFalse(self.state._requires_independent_audit(question, decision, high_risk_target))
        self.assertEqual(self.state._disabled_audit()["mode"], "disabled")

    def test_interactive_job_returns_accepted_and_progress_can_be_polled(self) -> None:
        body = {
            "questions": [{
                "exercise_id": "job-2529221",
                "question_press": "计算并化简含有分母有理化的根式",
                "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
            }],
        }
        status, submitted = self.request("POST", "/api/v1/classification-jobs", body)
        self.assertEqual(status, 202)
        for _ in range(20):
            status, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["completed"], 1)
        self.assertEqual(snapshot["results"][0]["exercise_id"], "job-2529221")

    def test_interactive_cloud_job_routes_then_classifies_with_one_topic_catalog(self) -> None:
        target = self.state.taxonomy.all_targets()[0]

        class StubCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            def __init__(self) -> None:
                self.calls: list[str] = []

            def route_fast_batch(self, questions, taxonomy, rules):
                self.calls.append("routing")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "routed",
                    "required_knowledge_points": ["实数运算"], "latest_topic_id": target.topic_id,
                    "confidence": 0.96, "reason": "路由完成", "review_reasons": [],
                } for question in questions]

            def classify_topic_batch(self, questions, topic_id, taxonomy, rules, routings):
                self.calls.append(f"topic:{topic_id}")
                self.assertEqual(topic_id, target.topic_id)
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.96, "reason": "唯一命中", "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    "routing": routings[str(question["exercise_id"])], "audit": None,
                } for question in questions]

            def audit_batch(self, items, taxonomy, rules):
                self.calls.append("audit")
                return [{
                    "exercise_id": str(question["exercise_id"]), "passed": True,
                    "violations": [], "reason": "未发现冲突", "confidence": 0.95,
                } for question, _routing, _decision, _target in items]

        cloud = StubCloud()
        # 供内部桩断言使用，避免闭包中依赖 unittest 的隐式绑定。
        cloud.assertEqual = self.assertEqual
        self.state.cloud = cloud
        self.state.settings["classifier"] = {"cloud": {"audit_mode": "always"}}
        body = {"questions": [{"exercise_id": "cloud-job-1", "question_press": "分母有理化"}]}
        status, submitted = self.request("POST", "/api/v1/classification-jobs", body)
        self.assertEqual(status, 202)
        for _ in range(20):
            _, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["results"][0]["target"]["level3_id"], target.level3_id)
        self.assertEqual(cloud.calls, ["routing", f"topic:{target.topic_id}", "audit"])

    def test_interactive_cloud_job_starts_topic_classification_before_all_routing_batches_finish(self) -> None:
        target = self.state.taxonomy.all_targets()[0]
        events: list[str] = []

        class StubCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            def route_fast_batch(self, questions, taxonomy, rules):
                if any(question["exercise_id"] == "pipe-0" for question in questions):
                    time.sleep(0.08)
                    events.append("routing-long-finished")
                else:
                    events.append("routing-short-finished")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "routed",
                    "required_knowledge_points": ["实数运算"], "latest_topic_id": target.topic_id,
                    "confidence": 0.96, "reason": "路由完成", "review_reasons": [],
                } for question in questions]

            def classify_topic_batch(self, questions, topic_id, taxonomy, rules, routings):
                events.append("classification-started")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.96, "reason": "唯一命中", "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    "routing": routings[str(question["exercise_id"])], "audit": None,
                } for question in questions]

            def audit_batch(self, items, taxonomy, rules):
                return [{
                    "exercise_id": str(question["exercise_id"]), "passed": True,
                    "violations": [], "reason": "未发现冲突", "confidence": 0.95,
                } for question, _routing, _decision, _target in items]

        self.state.settings.setdefault("classifier", {}).setdefault("cloud", {})["max_concurrent_requests"] = 2
        self.state.cloud = StubCloud()
        questions = [{"exercise_id": f"pipe-{index}", "question_press": "分母有理化"} for index in range(11)]
        _, submitted = self.request("POST", "/api/v1/classification-jobs", {"questions": questions})
        for _ in range(100):
            _, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(snapshot["status"], "completed")
        self.assertLess(events.index("classification-started"), events.index("routing-long-finished"))


if __name__ == "__main__":
    unittest.main()
